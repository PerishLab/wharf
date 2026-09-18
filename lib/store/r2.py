import datetime
import http.client
import os
from urllib.parse import quote, urlsplit

from dataclasses import dataclass, field

from lib.refusal import Conflict, Refusal
from lib.store.sigv4 import Credentials, Request, sign

VARIABLES = ("WHARF_R2_ENDPOINT", "WHARF_R2_BUCKET", "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY")


@dataclass(frozen=True)
class Operation:
    method: str
    key: str
    body: bytes = b""
    headers: dict = field(default_factory=dict)


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
        request = Request(operation.method, self.host, self.path(operation.key), operation.body, operation.headers)
        signed = sign(request, self.credentials, datetime.datetime.now(datetime.UTC))
        connection = self.connect(self.host, timeout=60)
        try:
            connection.request(operation.method, request.path, body=operation.body, headers=signed)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def exists(self, key):
        status, _ = self.request(Operation("HEAD", key))
        if status not in (200, 404):
            raise Refusal(f"HEAD {key} answered {status}")
        return status == 200

    def get(self, key):
        status, body = self.request(Operation("GET", key))
        if status != 200:
            raise Refusal(f"GET {key} answered {status}")
        return body

    def create(self, key, body, headers=None):
        status, answer = self.request(Operation("PUT", key, body, dict(headers or {}, **{"If-None-Match": "*"})))
        if status == 412:
            raise Conflict(f"{key} already exists")
        if status != 200:
            raise Refusal(f"PUT {key} answered {status}: {answer[:200]!r}")

    def put(self, key, body, headers=None):
        status, answer = self.request(Operation("PUT", key, body, dict(headers or {})))
        if status != 200:
            raise Refusal(f"PUT {key} answered {status}: {answer[:200]!r}")


def writer(bucket, role, environ=os.environ):
    names = ("WHARF_R2_ENDPOINT", f"WHARF_{role}_ACCESS_KEY_ID", f"WHARF_{role}_SECRET_ACCESS_KEY")
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise Refusal(f"missing {role.lower()} store configuration: {', '.join(missing)}")
    endpoint, access, secret = (environ[name] for name in names)
    return Bucket(endpoint, bucket, Credentials(access, secret))


def configured(environ=os.environ):
    missing = [name for name in VARIABLES if not environ.get(name)]
    if missing:
        raise Refusal(f"missing store configuration: {', '.join(missing)}")
    endpoint, name, access, secret = (environ[name] for name in VARIABLES)
    return Bucket(endpoint, name, Credentials(access, secret))
