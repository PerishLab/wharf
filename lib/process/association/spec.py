import http.client
import json
import ssl
import urllib.error
import urllib.request

from lib.refusal import Refusal
from lib.content import canonical, implementation, marker, resources
from lib.identity.bind.spec import entry, require, sha

POLICY = resources.read_json("manager/association.json")
TARGETS = POLICY["targets"]
SCHEMA = "wharf.binary.native.association/v2"
DISCOVERY = "wharf.binary.native.discovery/v2"
LIMIT = 256 * 1024 * 1024


def target(value):
    require(isinstance(value, str) and value in TARGETS, "native association target is unsupported")
    return TARGETS[value]


def code():
    return implementation.resourced(["lib.process.association.public", "lib.process.association.producer"], ["manager/association.json", "identity/format.json", "releases.json", "build/config.json", "manager/unix.sh.in", "manager/windows.ps1.in"])


def issuer():
    return {"policy": POLICY["schema"], "implementation": canonical.digest(code())}


def filename(name, selected):
    return name + "-" + selected + (".exe" if target(selected)["unit"] == "windows" else "")


def pairs(values):
    result = dict(values)
    require(len(result) == len(values), "native document has duplicate fields")
    return result


def document(raw):
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as failure:
        raise Refusal("native document is malformed") from failure


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        raise Refusal("native material redirects are unsupported")


class Unavailable(Refusal):
    def __init__(self, kind, details=None):
        require(kind in ("http", "timeout", "tls", "read"), "native transport category is unsupported")
        self.kind = kind
        self.details = {"stage": "material", "target": None, "marker": None, "status": None}
        self.details.update(details or {})
        require(set(self.details) == {"stage", "target", "marker", "status"}, "native transport context fields differ")
        require(self.details["stage"] in ("material", "distribution", "seal", "declaration", "discovery", "proof", "archive", "manager"), "native transport stage is unsupported")
        require(self.details["target"] is None or self.details["target"] in TARGETS, "native transport target is unsupported")
        version = self.details["marker"]
        require(version is None or isinstance(version, str) and len(version) <= 160 and marker.holds(version), "native transport marker is invalid")
        status = self.details["status"]
        require(status is None or kind == "http" and type(status) is int and 100 <= status <= 599, "native transport status is invalid")
        super().__init__("native material unavailable: " + kind + " at " + self.details["stage"])

    def during(self, stage=None, selected=None, version=None):
        details = dict(self.details)
        details.update({name: value for name, value in (("stage", stage), ("target", selected), ("marker", version)) if value is not None})
        return Unavailable(self.kind, details)


def unavailable(failure):
    if isinstance(failure, urllib.error.HTTPError):
        return Unavailable("http", {"status": failure.code if type(failure.code) is int and 100 <= failure.code <= 599 else None})
    reason = failure.reason if isinstance(failure, urllib.error.URLError) else failure
    kind = "tls" if isinstance(reason, ssl.SSLError) else "timeout" if isinstance(reason, TimeoutError) else "read"
    return Unavailable(kind)


def fetch(url, limit=1048576):
    require(isinstance(url, str) and url.startswith("https://") and type(limit) is int and 0 < limit <= 2 * LIMIT, "native material request is outside bounds")
    request = urllib.request.Request(url, headers={"User-Agent": "wharf", "Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    try:
        with opener.open(request, timeout=30) as reply:
            require(reply.status == 200 and reply.headers.get("Content-Encoding", "identity") == "identity", "native material reply is transformed or incomplete")
            raw = reply.read(limit + 1)
    except (OSError, http.client.HTTPException) as failure:
        raise unavailable(failure) from None
    require(0 < len(raw) <= limit, "native material reply exceeds bounds")
    return raw


def constant(value):
    raise Refusal("native document has non-JSON constants")
