import json
import re
import subprocess
import time
from pathlib import Path

from lib import parameters
from lib.content import marker
from lib.media.release import fetch, LAYOUT
from lib.process.trial import declaration
from lib.process.trial.declaration import checked
from lib.refusal import Refusal

REPOSITORY = "PerishLab/wharf"


def document(url):
    return json.loads(fetch(url))


def noted(requested, attempt):
    requested = requested if re.fullmatch(r"[1-9][0-9]*", str(requested)) else "unknown"
    attempt = attempt if re.fullmatch(r"[1-9][0-9]*", str(attempt)) else "unknown"
    return f"Windtun publisher run {requested} attempt {attempt}: "


def read(requested, attempt=None, timeout=30):
    prefix = noted(requested, attempt)
    checked(re.fullmatch(r"[1-9][0-9]*", str(requested)), "publisher run identity is invalid")
    checked(attempt is None or re.fullmatch(r"[1-9][0-9]*", str(attempt)), prefix + "native attempt identity is invalid")
    path = f"repos/{REPOSITORY}/actions/runs/{requested}" + (f"/attempts/{attempt}" if attempt is not None else "")
    try:
        raw = subprocess.check_output(["gh", "api", path], text=True, stderr=subprocess.PIPE, timeout=timeout)
        run = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError) as failure:
        raise Refusal(prefix + "native metadata read is unavailable or malformed") from failure
    checked(isinstance(run, dict), prefix + "native metadata is not an object")
    return run


def native(run, requested):
    prefix = noted(requested, run.get("run_attempt"))
    checked(str(run.get("id")) == requested, prefix + "native run identity differs")
    checked(re.fullmatch(r"[1-9][0-9]*", str(run.get("run_attempt"))), prefix + "native attempt identity is missing")
    for name in ("repository", "head_repository"):
        checked(isinstance(run.get(name), dict) and run[name].get("full_name") == REPOSITORY, prefix + f"native {name} differs")
    for name, expected in (("event", "workflow_dispatch"), ("path", ".github/workflows/ship.yml"), ("head_branch", "main")):
        checked(run.get(name) == expected, prefix + f"native {name} differs")
    checked(re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha"))), prefix + "native source identity is missing")


def source(identity):
    declaration.shape(identity, ("repository", "marker", "commit", "tree"), "Windtun publisher context")
    checked(re.fullmatch(r"PerishLab/[a-z0-9_-]+", str(identity["repository"])), "publisher product identity is missing")
    checked(all(re.fullmatch(r"[0-9a-f]{40}", str(identity[name])) for name in ("commit", "tree")), "publisher source identity is missing")
    marker.channel(identity["marker"])


def status(run):
    value = run.get("status")
    return value if value in ("queued", "waiting", "pending", "requested", "in_progress", "completed") else "unknown"


def candidate(run, requested, identity=None):
    native(run, requested)
    prefix = noted(requested, run.get("run_attempt"))
    if identity is not None:
        source(identity)
        checked(run.get("status") in ("in_progress", "completed"), prefix + f"native status {status(run)} is not started")
        return identity if marker.channel(identity["marker"]) in ("alpha", "beta", "rc") else None
    checked(run.get("status") == "completed", prefix + f"legacy discovery native status {status(run)} is not terminal")
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


def completed(requested, record):
    held = record.get("completed")
    declaration.shape(held, ("run", "attempt", "wharf"), "Windtun publication attempt")
    prefix = noted(requested, held["attempt"])
    checked(held["run"] == requested, prefix + "distribution belongs to another publisher run")
    checked(isinstance(held["attempt"], str) and re.fullmatch(r"[1-9][0-9]*", held["attempt"]), prefix + "publication attempt identity is invalid")
    checked(re.fullmatch(r"[0-9a-f]{40}", str(held["wharf"])), prefix + "publication control source is invalid")
    deadline = time.monotonic() + 180
    for number in range(19):
        remaining = deadline - time.monotonic()
        checked(remaining > 0, prefix + "native queued observation exceeded 180 seconds")
        run = read(requested, held["attempt"], min(30, remaining))
        native(run, requested)
        checked(str(run.get("run_attempt")) == held["attempt"], prefix + "native attempt differs from publication")
        checked(run.get("head_sha") == held["wharf"], prefix + "native source differs from publication")
        checked(time.monotonic() <= deadline, prefix + "native observation exceeded 180 seconds")
        state = run.get("status")
        if state in ("in_progress", "completed"):
            return run
        checked(state == "queued" and run.get("conclusion") is None, prefix + f"native status {status(run)} is not trusted")
        checked(number < 18, prefix + "native queued observation exhausted 18 read-only waits")
        remaining = deadline - time.monotonic()
        checked(remaining > 0, prefix + "native queued observation exceeded 180 seconds")
        time.sleep(min(10, remaining))


def selection(selected, raw):
    try:
        expected = json.loads(raw)
    except ValueError as error:
        raise Refusal("invalid Windtun selection") from error
    if expected is None:
        return
    declaration.shape(expected, ("repository", "marker"), "Windtun selection")
    checked(selected is not None and all(selected.get(name) == value for name, value in expected.items()), "publisher differs from the requested Windtun product or marker")


def chosen(held):
    try:
        identity = json.loads(held.get("publisher", "null"))
        expected = json.loads(held.get("selection", "null"))
    except ValueError as failure:
        raise Refusal("invalid Windtun publisher or selection") from failure
    if identity is not None:
        source(identity)
        selection(identity, held.get("selection", "null"))
    if expected is not None:
        declaration.shape(expected, ("repository", "marker"), "Windtun selection")
        checked(re.fullmatch(r"PerishLab/[a-z0-9_-]+", str(expected["repository"])), "Windtun selected repository is invalid")
        marker.channel(expected["marker"])
    selected = identity if identity is not None else expected
    if selected is not None and marker.channel(selected["marker"]) in ("alpha", "beta", "rc"):
        return selected, None
    run = read(held["run"])
    selected = candidate(run, held["run"], identity)
    selection(selected, held.get("selection", "null"))
    return selected, run


def admit(held):
    root = Path(held["source"]).resolve()
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=False)
    checked(re.fullmatch(r"[1-9][0-9]*", held["run"]) is not None, "publisher run must be an exact numeric ID")
    selected, run = chosen(held)
    result = {"schema": "wharf.trial.admission/v1", "eligible": False, "reason": "outside non-stable publication scope", "publisher": run}
    if selected:
        product = selected["repository"].split("/", 1)[1]
        authority = LAYOUT["authority"].format(name=product)
        version = selected["marker"]
        url = f"{authority}/v1/releases/{marker.channel(version)}/{version}/distribution.json"
        record = document(url)
        checked(isinstance(record, dict) and record.get("state") == "complete", "distribution is incomplete or malformed")
        checked(all(record.get(name) == value for name, value in selected.items()), "distribution identity differs")
        identity = {name: record.get(name) for name in ("repository", "marker", "commit", "tree")}
        source(identity)
        run = completed(held["run"], record)
        selected = candidate(run, held["run"], identity)
        checked(selected is not None, "publication attempt changed the selected release")
        result = dict(publication(run, selected, record), schema="wharf.trial.admission/v1")
    (evidence / "admission.json").write_text(json.dumps(result, indent=2) + "\n")
    parameters.answer({"eligible": str(result["eligible"]).lower()})
    return result
