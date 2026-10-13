import datetime
import http.client
import os
import re
from urllib.parse import quote

from lib.content import canonical
from lib.content.lane import codec, preview
from lib.content.static import assets, evidence
from lib.process.cfworker import apply
from lib.process.lane import build, entry, execution
from lib.refusal import Refusal
from lib.store import handoff, r2, workload
from lib.store.sigv4 import Credentials, Request, sign


class Reader:
    connect = http.client.HTTPSConnection

    def __init__(self, key, environ):
        self._prefix = workload.prefix(codec.read("digest", key))
        names = ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
        endpoint, bucket, access, secret = [r2.secret(environ, "WHARF_LANE_WORKLOAD_READER_" + name) for name in names]
        preview.matches(endpoint, re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com"), "lane workload origin")
        preview.matches(bucket, re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"), "lane workload bucket")
        if any(not value or any(not 33 <= ord(char) <= 126 for char in value) for value in (access, secret)):
            raise Refusal("lane workload requires independently prepared read credentials")
        self._host, self._bucket = endpoint[8:], bucket
        self._credentials = Credentials(access, secret)

    def get(self, key):
        suffix = key.removeprefix(self._prefix) if isinstance(key, str) else ""
        if key != self._prefix + suffix or (suffix not in ("record.json", "basis.json", "blobs/bundle.json") and not re.fullmatch(r"blobs/[0-9a-f]{64}", suffix)):
            raise Refusal("lane workload read is outside its exact recorded content")
        limit = assets.MAX_FILE if re.fullmatch(r"blobs/[0-9a-f]{64}", suffix) else 1048576
        path = "/" + self._bucket + "/" + quote(key, safe="/-_.~")
        headers = sign(Request("GET", self._host, path), self._credentials, datetime.datetime.now(datetime.UTC))
        connection = None
        try:
            connection = self.connect(self._host, timeout=30)
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status != 200 or response.getheader("Content-Encoding") not in (None, "identity") or response.getheader("Content-Range") is not None:
                raise Refusal("lane workload read is not exact untransformed recorded content")
            body = response.read(limit + 1)
            if not isinstance(body, bytes) or len(body) > limit:
                raise Refusal("lane workload read exceeds its object budget")
            return body
        except (OSError, http.client.HTTPException):
            raise Refusal("lane workload read failed; no private fallback or rebuild") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    raise Refusal("lane workload read cleanup failed") from None


def readonly(environ):
    forbidden = (*build.FORBIDDEN, "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN")
    if any(r2.secret(environ, name) for name in forbidden):
        raise Refusal("lane content consumption holds readers only, never writers")


def prepare(held, environ=os.environ):
    readonly(environ)
    intent = entry.requested(held["request"])
    before = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    value = before["prepared"]
    action = value["request"]["intent"]["action"]
    selected = held["workload"]
    plan = None
    if action == "deploy":
        selected = codec.read("digest", selected)
        if selected != value["request"]["intent"]["artifact"]["build"][apply.BUILD[0]]:
            raise Refusal("lane content selector differs from the exact reserved workload")
        document, files = handoff.fetch(Reader(selected, environ), selected)
        bodies = {item["blob"]: files[name] for name, item in document["files"].items()}
        plan = apply.prepare(value["registration"], value["current"], document, bodies.__getitem__)
    elif selected != "none":
        raise Refusal("lane inspect/dispose must not consume a newly built workload")
    after = execution.observe(intent, value, environ)
    if after != before:
        raise Refusal("lane execution authority changed during content consumption")
    return {"execution": canonical.digest(after), "workload": selected, "apply": plan}
