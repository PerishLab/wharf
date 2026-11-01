import json
import re
import subprocess

from lib.content import marker
from lib.identity.bind import control, verification
from lib.identity.bind.spec import FIELDS, require

PILOT = "PerishLab/plumb"


def api(path):
    raw = subprocess.check_output(["gh", "api", "--hostname", "github.com", path], text=True, timeout=30)
    require(len(raw.encode()) <= 1048576, "promotion source metadata exceeds bounds")
    return json.loads(raw)


def intent(source):
    root = f"repos/{source['repository']}"
    version = source["marker"]
    ref = api(f"{root}/git/ref/tags/{version}")
    require(ref.get("ref") == f"refs/tags/{version}" and ref.get("object", {}).get("type") == "tag", "stable intent needs its exact annotated tag")
    native = ref["object"]["sha"]
    require(isinstance(native, str) and re.fullmatch(r"[0-9a-f]{40}", native), "stable intent tag identity is invalid")
    tag = api(f"{root}/git/tags/{native}")
    require(tag.get("sha") == native and tag.get("tag") == version and tag.get("object") == {"sha": source["commit"], "type": "commit", "url": f"https://api.github.com/{root}/git/commits/{source['commit']}"}, "stable intent tag points to another source")
    lines = [line.strip() for line in str(tag.get("message", "")).splitlines() if line.strip()]
    require(len(lines) == 2 and lines[0] == version and lines[1].startswith("promotes "), "stable intent has no unambiguous prerelease")
    reference = lines[1].removeprefix("promotes ")
    require(marker.channel(reference) in ("rc", "beta") and marker.order(reference)[:3] == marker.order(version)[:3], "stable intent names another release line")
    return {"tag": native, "reference": reference}


def resolve(bucket, request, observer):
    require(request["repository"] == PILOT and marker.channel(request["marker"]) == "stable", "promotion preparation is limited to the Plumb stable pilot")
    require(request["run"] == observer["run"], "promotion preparation cannot replay another publication")
    control.observer(dict(observer, attempt=request["attempt"]))
    selected = dict(request, reference="")
    document, producer = verification.planned(bucket, selected)
    require(producer["wharf"] == observer["wharf"], "promotion plan belongs to another Wharf source")
    source = {name: document["context"][name] for name in FIELDS}
    declared = document["context"].get("binary_declaration")
    require(isinstance(declared, dict), "ordinary promotion plan has no frozen declaration")
    chosen = intent(source)
    return document, dict(selected, reference=chosen["reference"]), {"source": source, **chosen}


def unchanged(identity):
    require(intent(identity["source"]) == {name: identity[name] for name in ("tag", "reference")}, "stable intent moved during preparation")
