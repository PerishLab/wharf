import json
import os
import re
import time
from pathlib import Path

from lib import parameters
from lib.content import canonical, marker
from lib.identity.bind import control
from lib.identity.bind.spec import require
from lib.process.association import public
from lib.process.promotion import reader, subject

SCHEMA = "wharf.ship.request/v1"
INTENT = "wharf.ship.preparation.intent/v1"
DEFAULTS = {"run": "", "case": "declared", "reference": "", "candidate": False, "prepare": False, "proof": False, "attempt": "1"}
ROLES = {"request": "[verify] request", "prepared": "[read] prepared plan", "plan": "[plan] entries"}


def selection(value):
    require(isinstance(value, dict) and set(value) <= set(DEFAULTS) | {"repository", "marker"}, "dispatch fields differ")
    result = DEFAULTS | value
    require(all(isinstance(result.get(name), str) for name in ("repository", "marker", "run", "case", "reference", "attempt")), "dispatch text fields differ")
    for name in ("prepare", "candidate", "proof"):
        if result[name] in ("true", "false"):
            result[name] = result[name] == "true"
        require(type(result[name]) is bool, "dispatch modes must be boolean")
    marker.channel(result["marker"])
    if result["proof"]:
        require(not result["prepare"] and not result["candidate"] and result["run"] != "" and result["case"] == "declared" and result["attempt"] == "1", "proof replay needs a publication run without preparation, candidate or diagnostic selection")
    if result["prepare"]:
        require(not result["candidate"] and result["run"] == "" and result["case"] == "declared" and result["attempt"] == "1", "preparation conflicts with inspection, replay or diagnostic modes")
        require(marker.channel(result["marker"]) == "stable" and result["reference"] and marker.channel(result["reference"]) in ("rc", "beta"), "preparation needs real stable and non-stable intents")
    return result


def event():
    path = os.environ.get("GITHUB_EVENT_PATH")
    require(isinstance(path, str) and Path(path).is_file() and Path(path).stat().st_size <= 1048576, "native dispatch event is unavailable")
    return selection(json.loads(Path(path).read_bytes())["inputs"])


def native(run, identity):
    require(str(run.get("id")) == identity["run"] and str(run.get("run_attempt")) == identity["attempt"] and run.get("head_sha") == identity["wharf"], "dispatch native run or source differs")
    require(all(run.get(name, {}).get("id") == 1374358737 and run[name].get("full_name") == "PerishLab/wharf" for name in ("repository", "head_repository")), "dispatch repository differs")
    require(run.get("path") == ".github/workflows/ship.yml" and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main", "dispatch needs the fixed main ship workflow")


def active(raw, role):
    identity = control.observer(raw)
    require(role in ROLES and os.environ.get("GITHUB_JOB") == role, "preparation requires its fixed native role")
    repository = control.api("")
    require(repository.get("id") == 1374358737 and repository.get("node_id") == "R_kgDOUesM0Q" and repository.get("default_branch") == "main", "preparation repository authority differs")
    branch = control.api("branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "preparation requires protected main")
    for attempt in range(19):
        run = control.api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}")
        native(run, identity)
        if run.get("status") == "in_progress" and run.get("conclusion") is None:
            break
        require(run.get("status") == "queued" and run.get("conclusion") is None and attempt < 18, "preparation native run is not active")
        time.sleep(10)
    catalog = control.api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}/jobs?per_page=100")
    jobs = catalog.get("jobs")
    require(isinstance(jobs, list) and type(catalog.get("total_count")) is int and len(jobs) == catalog["total_count"] and 1 <= len(jobs) <= 100, "preparation native jobs are incomplete")
    require(all(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0 and str(job.get("run_id")) == identity["run"] and str(job.get("run_attempt")) == identity["attempt"] and job.get("head_sha") == identity["wharf"] and job.get("head_branch") == "main" for job in jobs) and len({job["id"] for job in jobs}) == len(jobs), "preparation native job identities differ")
    selected = [job for job in jobs if job.get("name") == ROLES[role]]
    require(len(selected) == 1 and selected[0].get("status") == "in_progress" and selected[0].get("conclusion") is None, "preparation native role is not active")
    control.started(selected[0].get("started_at"))
    return identity, jobs


def intent(selected):
    control.location(selected["repository"], selected["marker"])
    root = f"repos/{selected['repository']}"
    ref = subject.api(root + "/git/ref/tags/" + selected["marker"])
    require(ref.get("object", {}).get("type") == "tag", "preparation needs an annotated stable tag")
    tag = subject.api(root + "/git/tags/" + ref["object"]["sha"])
    commit = tag.get("object", {}).get("sha")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "preparation tag commit is invalid")
    held = subject.api(root + "/git/commits/" + commit)
    source = control.source({"repository": selected["repository"], "marker": selected["marker"], "commit": commit, "tree": held.get("tree", {}).get("sha")})
    chosen = subject.intent(source)
    require(chosen["tag"] == ref["object"]["sha"] and chosen["reference"] == selected["reference"], "preparation reference differs from annotated intent")
    reference = public.snapshot({"repository": selected["repository"], "marker": selected["reference"], "run": "null"})
    require(all(reference["source"][name] == source[name] for name in ("repository", "commit", "tree")), "preparation reference belongs to another source")
    return {"schema": INTENT, "source": source, "tag": chosen["tag"], "reference": selected["reference"], "publisher": reference["publisher"]}


def qualify(selected):
    chosen = intent(selected)
    qualified = reader.pins(reader.check(selected["repository"], selected["reference"]))
    require(qualified["source"] == dict(chosen["source"], marker=chosen["reference"]), "preparation qualification source differs")
    require(intent(selected) == chosen, "preparation intent changed during admission")
    return dict(chosen, qualification=qualified)


def request(held):
    identity, _ = active(held["observer"], "request")
    raw = json.loads(held["selection"])
    require(isinstance(raw, dict) and set(raw) == {"schema", "inputs"} and raw["schema"] == SCHEMA, "dispatch request schema differs")
    selected = selection(raw["inputs"])
    require(event() == selected, "dispatch projection differs from its actual native event")
    chosen = qualify(selected) if selected["prepare"] else None
    parameters.answer({"mode": "prepare" if selected["prepare"] else "ordinary", "preparation": canonical.encode(chosen).decode()})
    return {"schema": SCHEMA, "observer": identity, "preparation": chosen, "publication": False, "promotion": False}


def confirm(value, source):
    require(isinstance(value, dict) and set(value) == {"schema", "source", "tag", "reference", "publisher", "qualification"} and value["schema"] == INTENT and value["source"] == source, "prepared source differs from admitted intent")
    selected = {"repository": source["repository"], "marker": source["marker"], "reference": value["reference"]}
    current = qualify(selected)
    require(current == value, "preparation intent or current qualification changed")
    return value


def planning(held, source):
    active({name: held[name] for name in ("run", "attempt", "wharf")}, "plan")
    selected = event()
    require(selected["prepare"] and all(selected[name] == source[name] for name in ("repository", "marker")), "plan does not belong to actual preparation dispatch")
    return confirm(json.loads(held["preparation"]), source)
