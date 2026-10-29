import json
import datetime
import os
import re
import subprocess
import time

from lib.content import canonical, implementation
from lib.process.trial.admission import REPOSITORY, checked
from lib.process.trial.declaration import shape
from lib.refusal import Refusal

POLICY = "wharf.windtun.linux/v1"
NAMES = {"windtun-prepare": "[windtun] prepare attempt", "windtun": "[windtun] published artifacts", "windtun-issue": "[windtun] issue qualification"}


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def clock(value):
    checked(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value), "native attempt start is missing")
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC).timestamp()
    except ValueError as failure:
        raise Refusal("native attempt start is invalid") from failure


def decode(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    shape(value, ("run", "attempt", "wharf"), "Windtun observer")
    checked(all(re.fullmatch(r"[1-9][0-9]*", str(value[name])) for name in ("run", "attempt")), "observer run or attempt is invalid")
    checked(re.fullmatch(r"[0-9a-f]{40}", str(value["wharf"])), "observer source is invalid")
    return {name: str(value[name]) for name in ("run", "attempt", "wharf")}


def api(path):
    endpoint = f"repos/{REPOSITORY}" + ("/" + path if path else "")
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, timeout=30))


def native_jobs(observer):
    reply = api(f"actions/runs/{observer['run']}/attempts/{observer['attempt']}/jobs?per_page=100")
    jobs = reply.get("jobs")
    checked(isinstance(jobs, list) and reply.get("total_count") == len(jobs) and 1 <= len(jobs) <= 100, "observer job inventory is incomplete")
    found, identifiers = {}, set()
    for job in jobs:
        checked(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0, "observer job identity is invalid")
        checked(str(job.get("run_id")) == observer["run"] and str(job.get("run_attempt")) == observer["attempt"] and job.get("head_sha") == observer["wharf"] and job.get("head_branch") == "main", "observer job belongs to another source or attempt")
        checked(job["id"] not in identifiers, "observer job identity is duplicated")
        identifiers.add(job["id"])
        if job.get("name") in NAMES.values():
            checked(job["name"] not in found, "observer role is ambiguous")
            found[job["name"]] = job
    return found


def observe(raw, role):
    observer = decode(raw)
    checked(os.environ.get("GITHUB_JOB") == role, "Windtun action requires its fixed native job")
    repository = api("")
    checked(repository.get("full_name") == REPOSITORY and repository.get("default_branch") == "main", "observer repository authority differs")
    run = None
    for attempt in range(6):
        run = api(f"actions/runs/{observer['run']}/attempts/{observer['attempt']}")
        checked(str(run.get("id")) == observer["run"] and str(run.get("run_attempt")) == observer["attempt"], "observer run or attempt differs")
        checked(all(run.get(name, {}).get("id") == repository["id"] and run[name].get("full_name") == REPOSITORY for name in ("repository", "head_repository")), "observer repository or fork differs")
        checked(run.get("path") == ".github/workflows/ship.yml" and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and run.get("head_sha") == observer["wharf"], "observer is not exact trusted main ship code")
        if run.get("status") != "queued":
            break
        if attempt < 5:
            time.sleep(2)
    checked(run.get("status") == "in_progress" and run.get("conclusion") is None, "observer is not an active native attempt")
    jobs = native_jobs(observer)
    current = jobs.get(NAMES[role], {})
    checked(current.get("status") == "in_progress" and current.get("conclusion") is None, "observer native role is not active")
    return observer, jobs


def succeeded(job, steps):
    checked(job.get("status") == "completed" and job.get("conclusion") == "success", "trusted producer job did not complete successfully")
    held = job.get("steps")
    checked(isinstance(held, list), "trusted producer steps are missing")
    for name in steps:
        selected = [step for step in held if step.get("name") == name]
        checked(len(selected) == 1 and selected[0].get("status") == "completed" and selected[0].get("conclusion") == "success", "trusted producer step did not complete successfully")


def fingerprint():
    modules = [f"lib.process.trial.{name}" for name in ("context", "preparation", "transport", "credential", "publication", "consumer", "judgment", "admission", "declaration")]
    return implementation.resourced(modules, ["releases.json", "parameters.json"])


def address(prepared, kind):
    from lib.store import workload
    return workload.key({"schema": "wharf.windtun.workload/v1", "kind": kind, "prepared": prepared}, fingerprint())


def bundle(files, observer):
    return canonical.digest({"files": files, "observer": observer})
