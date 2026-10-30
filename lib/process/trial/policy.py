import base64
import hashlib
import json
import re
import subprocess

from lib.content import canonical, resources
from lib.process.trial import context
from lib.process.trial.admission import REPOSITORY, checked
from lib.refusal import Refusal

SCHEMA = "wharf.windtun.requirements/v1"
RESOURCE = "check/windtun.json"
CONTENTS = "contents/resources/check/windtun.json"
IDENTITY = {"id": 1374358737, "node_id": "R_kgDOUesM0Q", "full_name": REPOSITORY, "default_branch": "main", "visibility": "public", "archived": False, "disabled": False}
OWNER = {"id": 330236468, "node_id": "O_kgDOE68CNA", "login": "PerishLab"}


def expected():
    return {"schema": SCHEMA, "policy": context.POLICY, "implementation": canonical.digest(context.fingerprint())}


def decode(raw):
    checked(isinstance(raw, bytes) and 0 < len(raw) <= 4096, "Windtun requirements exceed document bounds")
    value = json.loads(raw)
    checked(isinstance(value, dict) and set(value) == {"schema", "policy", "implementation"}, "Windtun requirements fields differ")
    checked(raw == canonical.encode(value) and value["schema"] == SCHEMA and value["policy"] == context.POLICY, "Windtun requirements format or policy is unsupported")
    checked(context.digest(value["implementation"]), "Windtun requirements implementation is invalid")
    return value


def findings():
    try:
        checked(decode(resources.read_bytes(RESOURCE)) == expected(), "Windtun requirements differ from the actual validator")
        return []
    except (Refusal, OSError, ValueError, TypeError):
        return ["Windtun requirements do not match the actual validator; regenerate the derived resource"]


def api(path):
    raw = subprocess.check_output(["gh", "api", "--hostname", "github.com", f"repos/{REPOSITORY}" + ("/" + path if path else "")], timeout=30)
    return json.loads(raw)


def authority(value):
    checked(isinstance(value, dict) and all(type(value.get(name)) is type(expected) and value[name] == expected for name, expected in IDENTITY.items()), "Windtun requirements repository authority differs")
    owner = value.get("owner")
    checked(isinstance(owner, dict) and all(type(owner.get(name)) is type(expected) and owner[name] == expected for name, expected in OWNER.items()), "Windtun requirements owner authority differs")
    return dict(IDENTITY, owner=dict(OWNER))


def head(value):
    checked(isinstance(value, dict) and value.get("name") == "main" and value.get("protected") is True, "Windtun requirements need protected main")
    commit = value.get("commit", {}).get("sha")
    checked(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "Windtun requirements deployment identity is invalid")
    return commit


def content(value):
    checked(isinstance(value, dict) and value.get("type") == "file" and value.get("path") == CONTENTS.removeprefix("contents/") and value.get("encoding") == "base64", "Windtun requirements source entry differs")
    size, encoded, sha = value.get("size"), value.get("content"), value.get("sha")
    checked(type(size) is int and 0 < size <= 4096 and isinstance(encoded, str) and len(encoded) <= 6000, "Windtun requirements source exceeds bounds")
    checked(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), "Windtun requirements blob identity is invalid")
    raw = base64.b64decode("".join(encoded.splitlines()), validate=True)
    checked(len(raw) == size and hashlib.sha1(b"blob " + str(size).encode() + b"\0" + raw).hexdigest() == sha, "Windtun requirements source bytes differ from their Git blob")
    return raw, {"git": sha, "sha256": hashlib.sha256(raw).hexdigest(), "size": size}


def current():
    try:
        repository = authority(api(""))
        source = head(api("branches/main"))
        commit = api("git/commits/" + source)
        checked(isinstance(commit, dict) and commit.get("sha") == source, "Windtun requirements commit differs from main")
        tree = commit.get("tree", {}).get("sha")
        checked(isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{40}", tree), "Windtun requirements deployment tree is invalid")
        raw, blob = content(api(CONTENTS + "?ref=" + source))
        requirements = decode(raw)
        checked(head(api("branches/main")) == source and authority(api("")) == repository, "Windtun requirements deployment changed during readback")
        return {"schema": "wharf.windtun.requirements.readback/v1", "requirements": requirements, "deployment": {"repository": repository, "commit": source, "tree": tree, "path": CONTENTS.removeprefix("contents/"), "blob": blob}, "promotion": False}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as failure:
        raise Refusal("Windtun requirements are unavailable or malformed") from failure
