import json
import re
import subprocess
from pathlib import Path

from lib import parameters
from lib.content import marker
from lib.media.release import fetch, LAYOUT
from lib.process.trial import declaration
from lib.process.trial.declaration import checked

REPOSITORY = "PerishLab/wharf"


def document(url):
    return json.loads(fetch(url))


def candidate(run, requested):
    checked(str(run.get("id")) == requested, "publisher run identity differs")
    checked(run.get("repository", {}).get("full_name") == REPOSITORY, "publisher repository differs")
    checked(run.get("head_repository", {}).get("full_name") == REPOSITORY, "publisher source repository differs")
    checked(run.get("event") == "workflow_dispatch" and run.get("path") == ".github/workflows/ship.yml", "publisher is not the trusted ship workflow")
    checked(run.get("head_branch") == "main" and re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha"))), "publisher source is not exact main control code")
    checked(run.get("status") == "completed", "publisher is not terminal")
    parts = str(run.get("display_title")).split()
    checked(len(parts) == 3 and parts[0] == "[ship]" and re.fullmatch(r"PerishLab/[a-z0-9_-]+", parts[1]), "publisher title has no exact product release identity")
    repository, version = parts[1:]
    return {"repository": repository, "marker": version} if marker.channel(version) in ("alpha", "beta", "rc") else None


def publication(run, selected, record):
    expected = selected
    version = selected["marker"]
    checked(all(record.get(name) == value for name, value in expected.items()), "distribution identity differs")
    checked(record.get("state") == "complete", "distribution is incomplete")
    checked(record.get("completed") == {"run": str(run["id"]), "attempt": str(run["run_attempt"]), "wharf": run["head_sha"]}, "distribution belongs to another publisher run or attempt")
    checked(re.fullmatch(r"[0-9a-f]{40}", str(record.get("commit"))) is not None, "distribution source is missing")
    source = {name: record.get(name) for name in ("repository", "marker", "commit", "tree")}
    checked(re.fullmatch(r"[0-9a-f]{40}", str(source["tree"])) is not None, "distribution tree is missing")
    if "trial" not in record:
        return {"eligible": False, "state": "unrecorded", "reason": "publisher did not record a Seatrial declaration", "distribution": record, "publisher": run}
    trial = record["trial"]
    if trial is None:
        return {"eligible": False, "state": "undeclared", "reason": "product release declares no Seatrial suite", "distribution": record, "publisher": run}
    checked(record.get("media", {}).get("binaries") in ("published", "present"), "distribution has no published executable")
    declaration.bound(trial, source)
    product = selected["repository"].split("/", 1)[1]
    authority = LAYOUT["authority"].format(name=product)
    checked(trial["authority"] == authority, "declared product authority differs from distribution")
    return {"eligible": True, "product": product, "authority": authority, "trial": trial, "marker": version, "channel": marker.channel(version), "commit": record["commit"], "distribution": record, "publisher": run}


def admit(held):
    root = Path(held["source"]).resolve()
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=False)
    checked(re.fullmatch(r"[1-9][0-9]*", held["run"]) is not None, "publisher run must be an exact numeric ID")
    raw = subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{held['run']}"], text=True)
    run = json.loads(raw)
    selected = candidate(run, held["run"])
    result = {"schema": "wharf.trial.admission/v1", "eligible": False, "reason": "outside non-stable publication scope", "publisher": run}
    if selected:
        product = selected["repository"].split("/", 1)[1]
        authority = LAYOUT["authority"].format(name=product)
        version = selected["marker"]
        url = f"{authority}/v1/releases/{marker.channel(version)}/{version}/distribution.json"
        result = dict(publication(run, selected, document(url)), schema="wharf.trial.admission/v1")
    (evidence / "admission.json").write_text(json.dumps(result, indent=2) + "\n")
    parameters.answer({"eligible": str(result["eligible"]).lower()})
    return result
