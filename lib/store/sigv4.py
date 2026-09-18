import hashlib
import hmac
from dataclasses import dataclass, field

REGION = "auto"
SERVICE = "s3"


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class Request:
    method: str
    host: str
    path: str
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    query: str = ""


def mac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def canonical(request, headers, payload):
    names = sorted(headers)
    lines = "".join(f"{name}:{str(headers[name]).strip()}\n" for name in names)
    return "\n".join([request.method, request.path, request.query, lines, ";".join(names), payload]), ";".join(names)


def sign(request, credentials, now):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    payload = hashlib.sha256(request.body).hexdigest()
    headers = {"host": request.host, "x-amz-content-sha256": payload, "x-amz-date": stamp}
    headers.update({name.lower(): value for name, value in request.headers.items()})
    text, signed = canonical(request, headers, payload)
    scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
    key = mac(mac(mac(mac(("AWS4" + credentials.secret_key).encode(), day), REGION), SERVICE), "aws4_request")
    statement = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(text.encode()).hexdigest()}"
    signature = hmac.new(key, statement.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    return headers
