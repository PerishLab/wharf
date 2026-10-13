import datetime
import http.client
import os
import re
import xml.etree.ElementTree as ElementTree

from lib.content import canonical
from lib.content.lane import codec, preview
from lib.content.static import evidence
from lib.process.lane import build, entry, execution
from lib.refusal import Conflict, Refusal
from lib.store import r2
from lib.store.lane import documents, public, publication
from lib.store.sigv4 import Credentials, Request, sign


def confirm(before, environ):
    if execution.observe(before["intent"], before["execution"]["prepared"], environ) != before["execution"]:
        raise Refusal("lane evidence original-run authority or exact reservation moved")


def configured(environ, role="EVIDENCE"):
    names = ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
    endpoint, bucket, access, secret = (r2.secret(environ, "WHARF_LANE_" + role + "_" + name) for name in names)
    preview.matches(endpoint, re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com"), "lane evidence store origin")
    preview.matches(bucket, re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"), "lane evidence store bucket")
    if any(not value or any(not 33 <= ord(char) <= 126 for char in value) for value in (access, secret)):
        raise Refusal("lane evidence write requires independently prepared credentials")
    return Bucket(endpoint, bucket, Credentials(access, secret))


class Bucket(r2.Bucket):
    def conditional(self, operation):
        status, answer, _ = self.request(operation)
        if status == 412 or status == 409 and locked(answer):
            raise Conflict("lane evidence immutable key requires exact collision readback")
        if status != 200:
            raise Refusal("lane evidence create response is unverified; no retry")

    def permitted(self, operation):
        if operation.query or operation.method not in ("GET", "PUT") or operation.method == "PUT" and operation.headers.get("If-None-Match") != "*":
            raise Refusal("lane evidence store accepts only exact reads and create-only writes")

    def request(self, operation):
        self.permitted(operation)
        path = self.path(operation.key)
        signed = sign(Request(operation.method, self.host, path, operation.body, operation.headers), self.credentials, datetime.datetime.now(datetime.UTC))
        connection = None
        try:
            connection = self.connect(self.host, timeout=30)
            connection.request(operation.method, path, body=operation.body, headers=signed)
            response = connection.getresponse()
            if response.getheader("Content-Encoding") not in (None, "identity"):
                raise Refusal("lane evidence store cannot transform its response")
            body = response.read(public.LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > public.LIMIT:
                raise Refusal("lane evidence store response exceeds its document budget")
            return response.status, body, {name.lower(): value for name, value in response.getheaders()}
        except (OSError, http.client.HTTPException):
            raise Refusal("lane evidence store response is unverified; no retry") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    raise Refusal("lane evidence store cleanup is unverified") from None


def locked(body):
    try:
        text = body.decode("utf-8")
        if "<!" in text or "\x00" in text:
            return False
        root = ElementTree.fromstring(text)
    except (UnicodeDecodeError, ElementTree.ParseError):
        return False
    codes = root.findall("Code")
    return root.tag == "Error" and len(codes) == 1 and len(codes[0]) == 0 and codes[0].text == "ObjectLockedByBucketPolicy"


class Writer:
    def __init__(self, before, selected, environ):
        self._before, self._selected, self._environ = before, selected, environ
        self._bucket = configured(environ)

    def create(self, key, body, headers):
        if key != self._selected["key"] or body != self._selected["body"]:
            raise Refusal("lane evidence write differs from its exact immutable record")
        confirm(self._before, self._environ)
        try:
            self._bucket.create(key, body, headers)
        except Conflict:
            raise
        except (OSError, http.client.HTTPException, Refusal):
            raise Refusal("lane evidence write is unverified; no retry or inferred completion") from None
        confirm(self._before, self._environ)

    def get(self, key):
        if key != self._selected["key"]:
            raise Refusal("lane evidence collision read is outside its exact immutable key")
        confirm(self._before, self._environ)
        try:
            body = self._bucket.get(key)
        except (OSError, http.client.HTTPException, Refusal):
            raise Refusal("lane evidence collision read is unavailable; no fallback") from None
        confirm(self._before, self._environ)
        return body


def prepare(held, environ):
    forbidden = tuple(name for name in build.FORBIDDEN if not name.startswith("WHARF_LANE_EVIDENCE_")) + (
        "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN",
        "WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_LANE_PROVIDER_READER_TOKEN",
        "WHARF_LANE_WORKLOAD_READER_ACCESS_KEY_ID", "WHARF_LANE_WORKLOAD_READER_SECRET_ACCESS_KEY")
    if any(r2.secret(environ, name) for name in forbidden):
        raise Refusal("lane evidence job holds only its evidence writer and required readers")
    if environ.get("GITHUB_JOB") != "evidence":
        raise Refusal("lane evidence publication requires its fixed original job")
    intent = entry.requested(held["request"])
    observed = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    value = observed["prepared"]
    supplied = documents.object_value(evidence.decode(held["provider"]), ("execution", "plan", "operation"))
    operation = codec.read("operation", supplied["operation"])
    if supplied["execution"] != canonical.digest(observed) or any(operation[key] != value[key] for key in ("request", "assessment")):
        raise Refusal("lane evidence result differs from the entire exact original execution")
    declared = value["registration"]["declaration"]
    reader = public.prepare({"authority": r2.secret(environ, "WHARF_LANE_PUBLICATION_AUTHORITY"),
                             "origin": r2.secret(environ, "WHARF_LANE_PUBLICATION_ORIGIN")})
    record = publication.binding(declared, {"schema": 2, "entry": {"kind": "operation", "value": operation}})
    reference = publication.authority(reader, declared, reader.reference(codec.digest("record", record)))
    before = {"intent": intent, "execution": observed}
    confirm(before, environ)
    return before, record, reader, reference


def perform(held, environ=os.environ):
    before, record, reader, reference = prepare(held, environ)
    declared = before["execution"]["prepared"]["registration"]["declaration"]
    selected = {"key": public.location(reference), "body": codec.encode("record", record)}
    writer = Writer(before, selected, environ)
    result = publication.publish(writer, reader, declared, record)
    verified = publication.readback(reader, declared, result, record["entry"]["value"]["request"]["context"])
    if result != reference or verified != record:
        raise Refusal("lane evidence readback differs from the entire exact operation")
    confirm(before, environ)
    return result
