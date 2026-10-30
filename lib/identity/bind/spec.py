import hashlib
import re

from lib.content import canonical, implementation
from lib.refusal import Refusal

TARGET = "x86_64-unknown-linux-gnu"
POLICY = "wharf.binary.linux/v1"
FIELDS = ("repository", "marker", "commit", "tree")
CARGO = (["lib.cargo.basis", "lib.cargo.build"], ["build.json", "identity/format.json"])
BIND = (["lib.identity.bind"], ["identity/format.json"])
LIMIT = 256 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def sha(body):
    return hashlib.sha256(body).hexdigest()


def entry(body):
    return {"size": len(body), "sha256": sha(body)}


def code():
    modules = [f"lib.identity.bind.{name}" for name in ("spec", "control", "inventory", "storage", "artifacts", "producer", "comparison")]
    return implementation.resourced(modules, ["build.json", "identity/format.json", "releases.json"])


def issuer():
    return {"policy": POLICY, "implementation": canonical.digest(code())}
