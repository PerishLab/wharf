import datetime
import http.client
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from dataclasses import dataclass, field

from lib.refusal import Conflict, Refusal
from lib.content import preview
from lib.store.sigv4 import Credentials, Request, sign

VARIABLES = ("WHARF_R2_ENDPOINT", "WHARF_R2_BUCKET", "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY")


@dataclass(frozen=True)
class Operation:
    method: str
    key: str
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)


class Bucket:
    connect = http.client.HTTPSConnection

    def __init__(self, endpoint, name, credentials):
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
            raise Refusal(f"R2 endpoint {endpoint!r} must be a bare https origin")
        if not name or not credentials.access_key or not credentials.secret_key:
            raise Refusal("R2 bucket name and credentials are required")
        self.host = parts.hostname
        self.name = name
        self.credentials = credentials

    def path(self, key):
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise Refusal(f"object key {key!r} is not a plain relative key")
        return "/" + quote(self.name) + "/" + quote(key, safe="/-_.~")

    def request(self, operation):
        query = urlencode(sorted(operation.query.items()), quote_via=quote, safe="-_.~")
        path = self.path(operation.key) if operation.key else "/" + quote(self.name)
        request = Request(operation.method, self.host, path, operation.body, operation.headers, query)
        signed = sign(request, self.credentials, datetime.datetime.now(datetime.UTC))
        connection = self.connect(self.host, timeout=60)
        try:
            connection.request(operation.method, path + (f"?{query}" if query else ""), body=operation.body, headers=signed)
            response = connection.getresponse()
            return response.status, response.read(), {name.lower(): value for name, value in response.getheaders()}
        finally:
            connection.close()

    def head(self, key):
        status, _, headers = self.request(Operation("HEAD", key))
        if status not in (200, 404):
            raise Refusal(f"HEAD {key} answered {status}")
        return headers.get("etag") if status == 200 else None

    def exists(self, key):
        return self.head(key) is not None

    def get(self, key):
        status, body, _ = self.request(Operation("GET", key))
        if status != 200:
            raise Refusal(f"GET {key} answered {status}")
        return body

    def snapshot(self, key):
        status, body, headers = self.request(Operation("GET", key))
        if status == 404:
            return None, None
        if status != 200 or not headers.get("etag"):
            raise Refusal(f"GET {key} cannot provide a versioned snapshot: {status}")
        return body, headers["etag"]

    def create(self, key, body, headers=None):
        self.conditional(Operation("PUT", key, body, dict(headers or {}, **{"If-None-Match": "*"})))

    def swap(self, key, body, etag):
        condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
        self.conditional(Operation("PUT", key, body, dict({"Content-Type": "application/json"}, **condition)))

    def conditional(self, operation):
        status, answer, _ = self.request(operation)
        if status == 412:
            raise Conflict(f"{operation.key} changed or already exists")
        if status != 200:
            raise Refusal(f"PUT {operation.key} answered {status}: {answer[:200]!r}")

    def put(self, key, body, headers=None):
        status, answer, _ = self.request(Operation("PUT", key, body, dict(headers or {})))
        if status != 200:
            raise Refusal(f"PUT {key} answered {status}: {answer[:200]!r}")

    def copy(self, source, key):
        headers = {"x-amz-copy-source": self.path(source), "If-None-Match": "*"}
        status, answer, _ = self.request(Operation("PUT", key, b"", headers))
        if status not in (200, 412):
            raise Refusal(f"COPY {source} to {key} answered {status}: {answer[:200]!r}")

    def prefixes(self, prefix):
        found, token = [], None
        while True:
            query = {"list-type": "2", "prefix": prefix, "delimiter": "/", **({"continuation-token": token} if token else {})}
            status, body, _ = self.request(Operation("GET", "", query=query))
            if status != 200:
                raise Refusal(f"LIST {prefix} answered {status}")
            root = ElementTree.fromstring(body)
            space = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            found += [node.findtext(f"{space}Prefix") for node in root.iter(f"{space}CommonPrefixes")]
            token = root.findtext(f"{space}NextContinuationToken")
            if not token:
                return found


def writer(bucket, role, environ=os.environ):
    names = ("WHARF_R2_ENDPOINT", f"WHARF_{role}_ACCESS_KEY_ID", f"WHARF_{role}_SECRET_ACCESS_KEY")
    held = [secret(environ, name) for name in names]
    missing = [name for name, value in zip(names, held) if not value]
    if missing:
        raise Refusal(f"missing {role.lower()} store configuration: {', '.join(missing)}")
    endpoint, access, key = held
    return Bucket(endpoint, bucket, Credentials(access, key))


def secret(environ, name):
    if environ.get(name):
        return environ[name]
    held = environ.get(f"{name}_FILE")
    return Path(held).read_text().strip() if held and Path(held).is_file() else ""


def configured(environ=os.environ):
    missing = [name for name in VARIABLES if not environ.get(name)]
    if missing:
        raise Refusal(f"missing store configuration: {', '.join(missing)}")
    endpoint, name, access, secret = (environ[name] for name in VARIABLES)
    return Bucket(endpoint, name, Credentials(access, secret))


class Registration:
    connect = http.client.HTTPSConnection

    def __init__(self, configuration, selection):
        preview.shape(configuration, {"endpoint", "bucket", "access", "secret"}, "registration reader configuration")
        preview.shape(selection, {"repository", "app"}, "registration reader selection")
        preview.matches(selection["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "registration repository")
        preview.slug(selection["app"], "registration app")
        preview.matches(configuration["endpoint"], re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com"), "registration R2 origin")
        preview.matches(configuration["bucket"], re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"), "registration bucket")
        for key in ("access", "secret"):
            if not isinstance(configuration[key], str) or not configuration[key] or any(ord(char) < 33 or ord(char) > 126 for char in configuration[key]):
                raise Refusal("Preview registration requires prepared read credentials")
        self._host = urlsplit(configuration["endpoint"]).hostname
        self._bucket = configuration["bucket"]
        self._credentials = Credentials(configuration["access"], configuration["secret"])
        self._key = f"preview/v1/registrations/{selection['repository']}/{selection['app']}.json"

    def get(self, key):
        if key != self._key:
            raise Refusal("Preview registration read is outside its selected key")
        path = "/" + self._bucket + "/" + quote(key, safe="/-_.~")
        signed = sign(Request("GET", self._host, path), self._credentials, datetime.datetime.now(datetime.UTC))
        connection = None
        try:
            connection = self.connect(self._host, timeout=30)
            connection.request("GET", path, headers=signed)
            response = connection.getresponse()
            if response.status != 200:
                raise Refusal("Preview registration read is unavailable; no target inferred")
            body = response.read(65537)
            if not isinstance(body, bytes) or len(body) > 65536:
                raise Refusal("Preview registration read exceeds its bounded document budget")
            return body
        except (OSError, http.client.HTTPException) as error:
            raise Refusal("Preview registration read failed; no target inferred") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException) as error:
                    raise Refusal("Preview registration connection cleanup failed") from error


def registration(selection, environ=os.environ):
    names = {"endpoint": "WHARF_PREVIEW_REGISTRATION_ENDPOINT", "bucket": "WHARF_PREVIEW_REGISTRATION_BUCKET",
             "access": "WHARF_PREVIEW_REGISTRATION_ACCESS_KEY_ID", "secret": "WHARF_PREVIEW_REGISTRATION_SECRET_ACCESS_KEY"}
    return Registration({key: secret(environ, name) for key, name in names.items()}, selection)
