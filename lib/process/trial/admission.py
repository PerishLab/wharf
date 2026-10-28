import json
import re
import subprocess
from pathlib import Path

from lib import parameters
from lib.content import marker
from lib.media.release import fetch
from lib.refusal import Refusal

REPOSITORY = "PerishLab/wharf"
AUTHORITY = "https://releases.plumb.perish.uk"


def checked(condition, reason):
    if not condition:
        raise Refusal(reason)


def document(url):
    return json.loads(fetch(url))


def candidate(run, requested):
    checked(str(run.get("id")) == requested, "publisher run identity differs")
    checked(run.get("repository", {}).get("full_name") == REPOSITORY, "publisher repository differs")
    checked(run.get("head_repository", {}).get("full_name") == REPOSITORY, "publisher source repository differs")
    checked(run.get("event") == "workflow_dispatch" and run.get("path") == ".github/workflows/ship.yml", "publisher is not the trusted ship workflow")
    checked(run.get("head_branch") == "main" and re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha"))), "publisher source is not exact main control code")
    checked(run.get("status") == "completed", "publisher is not terminal")
    title = str(run.get("display_title"))
    if not title.startswith("[ship] PerishLab/plumb "):
        return None
    version = title.removeprefix("[ship] PerishLab/plumb ")
    return version if marker.channel(version) in ("alpha", "beta", "rc") else None


def publication(run, version, record):
    expected = {"repository": "PerishLab/plumb", "marker": version}
    checked(all(record.get(name) == value for name, value in expected.items()), "distribution identity differs")
    checked(record.get("state") == "complete", "distribution is incomplete")
    checked(record.get("completed") == {"run": str(run["id"]), "attempt": str(run["run_attempt"]), "wharf": run["head_sha"]}, "distribution belongs to another publisher run or attempt")
    checked(re.fullmatch(r"[0-9a-f]{40}", str(record.get("commit"))) is not None, "distribution source is missing")
    checked(record.get("media", {}).get("binaries") in ("published", "present"), "distribution has no published executable")
    return {"marker": version, "channel": marker.channel(version), "commit": record["commit"], "distribution": record, "publisher": run}


def admit(held):
    root = Path(held["source"]).resolve()
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=False)
    checked(re.fullmatch(r"[1-9][0-9]*", held["run"]) is not None, "publisher run must be an exact numeric ID")
    raw = subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{held['run']}"], text=True)
    run = json.loads(raw)
    version = candidate(run, held["run"])
    result = {"schema": "wharf.trial.admission/v1", "eligible": False, "reason": "outside initial Plumb non-stable scope", "publisher": run}
    if version:
        url = f"{AUTHORITY}/v1/releases/{marker.channel(version)}/{version}/distribution.json"
        result = dict(publication(run, version, document(url)), schema="wharf.trial.admission/v1", eligible=True)
    (evidence / "admission.json").write_text(json.dumps(result, indent=2) + "\n")
    parameters.answer({"eligible": str(result["eligible"]).lower()})
    return result
