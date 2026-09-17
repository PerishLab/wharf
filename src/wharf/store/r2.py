"""R2 through its S3 API, signed with SigV4 using only the standard library.

Writes are create-only: an object that already exists is never replaced, so a
second writer with different bytes surfaces as a Conflict instead of silently
winning.
"""

import datetime
import hashlib
import hmac
import http.client
from urllib.parse import quote, urlsplit

from wharf.refusal import Refusal

REGION = "auto"
SERVICE = "s3"


class Conflict(Refusal):
    """The key already holds an object; create-only writes never overwrite."""


def _hmac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


class Bucket:
    def __init__(self, endpoint, name, access_key, secret_key, connect=http.client.HTTPSConnection):
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
            raise Refusal(f"R2 endpoint {endpoint!r} must be a bare https origin")
        if not name or not access_key or not secret_key:
            raise Refusal("R2 bucket name and credentials are required")
        self.host = parts.hostname
        self.name = name
        self.access_key = access_key
        self.secret_key = secret_key
        self.connect = connect

    def _path(self, key):
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise Refusal(f"object key {key!r} is not a plain relative key")
        return "/" + quote(self.name) + "/" + quote(key, safe="/-_.~")

    def _headers(self, method, path, body, extra, now):
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        day = stamp[:8]
        payload = hashlib.sha256(body).hexdigest()
        headers = {"host": self.host, "x-amz-content-sha256": payload, "x-amz-date": stamp}
        headers.update({name.lower(): value for name, value in extra.items()})
        names = sorted(headers)
        canonical = "\n".join([
            method,
            path,
            "",
            "".join(f"{name}:{str(headers[name]).strip()}\n" for name in names),
            ";".join(names),
            payload,
        ])
        scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        signing = _hmac(_hmac(_hmac(_hmac(("AWS4" + self.secret_key).encode(), day), REGION), SERVICE), "aws4_request")
        signature = hmac.new(signing, f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{digest}".encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        )
        return headers

    def _request(self, method, key, body=b"", extra=None, now=None):
        path = self._path(key)
        headers = self._headers(method, path, body, extra or {}, now or datetime.datetime.now(datetime.UTC))
        connection = self.connect(self.host, timeout=60)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def exists(self, key):
        status, _ = self._request("HEAD", key)
        if status == 200:
            return True
        if status == 404:
            return False
        raise Refusal(f"HEAD {key} answered {status}")

    def get(self, key):
        status, body = self._request("GET", key)
        if status != 200:
            raise Refusal(f"GET {key} answered {status}")
        return body

    def create(self, key, body):
        status, answer = self._request("PUT", key, body, {"If-None-Match": "*"})
        if status == 412:
            raise Conflict(f"{key} already exists")
        if status != 200:
            raise Refusal(f"PUT {key} answered {status}: {answer[:200]!r}")
