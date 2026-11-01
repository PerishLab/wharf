import json
import urllib.request

from lib.refusal import Refusal
from lib.content import canonical, implementation, resources
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
    return implementation.resourced(["lib.process.association.public", "lib.process.association.producer"], ["manager/association.json", "identity/format.json", "releases.json", "build.json", "manager/unix.sh.in", "manager/windows.ps1.in"])


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


def fetch(url, limit=1048576):
    require(isinstance(url, str) and url.startswith("https://") and type(limit) is int and 0 < limit <= 2 * LIMIT, "native material request is outside bounds")
    request = urllib.request.Request(url, headers={"User-Agent": "wharf", "Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    with opener.open(request, timeout=30) as reply:
        require(reply.status == 200 and reply.headers.get("Content-Encoding", "identity") == "identity", "native material reply is transformed or incomplete")
        raw = reply.read(limit + 1)
    require(0 < len(raw) <= limit, "native material reply exceeds bounds")
    return raw


def constant(value):
    raise Refusal("native document has non-JSON constants")
