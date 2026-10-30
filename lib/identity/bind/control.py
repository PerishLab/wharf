import datetime
import json
import os
import re
import subprocess

from lib.content import canonical, marker
from lib.identity.bind.spec import FIELDS, require, sha
from lib.media import release

REPOSITORY = "PerishLab/wharf"
ROLE = "build-proof"
JOB = "[prove] published binary"
ROLES = {ROLE: JOB, "build-candidate": "[prove] candidate binary"}


def api(path):
    endpoint = f"repos/{REPOSITORY}" + ("/" + path if path else "")
    return json.loads(subprocess.check_output(["gh", "api", "--hostname", "github.com", endpoint], text=True, timeout=30))


def source(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS), "binary proof source fields differ")
    require(re.fullmatch(r"PerishLab/[a-z0-9_-]+", str(value["repository"])), "binary proof repository differs")
    marker.channel(value["marker"])
    require(all(re.fullmatch(r"[0-9a-f]{40}", str(value[name])) for name in ("commit", "tree")), "binary proof source identity is invalid")
    return value


def observer(value):
    value = json.loads(value) if isinstance(value, str) else value
    require(isinstance(value, dict) and set(value) == {"run", "attempt", "wharf"}, "binary proof observer fields differ")
    require(all(re.fullmatch(r"[1-9][0-9]*", str(value[name])) for name in ("run", "attempt")) and re.fullmatch(r"[0-9a-f]{40}", str(value["wharf"])), "binary proof observer identity is invalid")
    return {name: str(value[name]) for name in value}


def trusted(identity):
    identity = observer(identity)
    run = api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}")
    require(str(run.get("id")) == identity["run"] and str(run.get("run_attempt")) == identity["attempt"] and run.get("head_sha") == identity["wharf"], "binary proof producer run or attempt differs")
    require(all(run.get(name, {}).get("full_name") == REPOSITORY for name in ("repository", "head_repository")), "binary proof producer repository differs")
    require(run.get("path") == ".github/workflows/ship.yml" and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and run.get("status") in ("in_progress", "completed"), "binary proof requires trusted main ship code")
    return run


def started(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value), "binary proof native start is missing")
    datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def active(raw, role=ROLE):
    identity = observer(raw)
    require(role in ROLES and os.environ.get("GITHUB_JOB") == role, "binary proof requires its fixed native job")
    run = trusted(identity)
    repository = api("")
    require(repository.get("full_name") == REPOSITORY and repository.get("default_branch") == "main" and all(run[name].get("id") == repository.get("id") for name in ("repository", "head_repository")), "binary proof repository authority differs")
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "binary proof observer is not active")
    result = api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}/jobs?per_page=100")
    jobs = result.get("jobs")
    require(isinstance(jobs, list) and len(jobs) == result.get("total_count") and 1 <= len(jobs) <= 100, "binary proof job inventory is incomplete")
    require(all(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0 and str(job.get("run_id")) == identity["run"] and str(job.get("run_attempt")) == identity["attempt"] and job.get("head_sha") == identity["wharf"] and job.get("head_branch") == "main" for job in jobs), "binary proof job inventory belongs to another source or attempt")
    require(len({job["id"] for job in jobs}) == len(jobs), "binary proof job inventory has duplicate identities")
    selected = [job for job in jobs if job.get("name") == ROLES[role]]
    require(len(selected) == 1, "binary proof native role is ambiguous")
    job = selected[0]
    require(str(job.get("run_id")) == identity["run"] and str(job.get("run_attempt")) == identity["attempt"] and job.get("head_sha") == identity["wharf"] and job.get("head_branch") == "main" and job.get("status") == "in_progress" and job.get("conclusion") is None, "binary proof native role is not active for this source")
    return identity, started(job.get("started_at"))


def location(repository, version):
    require(re.fullmatch(r"PerishLab/[a-z0-9_-]+", str(repository)), "binary proof repository is invalid")
    channel = marker.channel(version)
    _, bucket, authority = release.place(release.Release(repository, version, "", ""))
    return bucket, authority, f"v1/releases/{channel}/{version}"


def public(held):
    _, authority, root = location(held["repository"], held["marker"])
    raw = release.fetch(f"{authority}/{root}/distribution.json")
    record = json.loads(raw)
    identity = source({name: record.get(name) for name in FIELDS})
    require(identity["repository"] == held["repository"] and identity["marker"] == held["marker"] and record.get("state") == "complete", "binary proof needs the exact complete distribution")
    require(record.get("media", {}).get("binaries") in ("published", "present"), "binary proof has no distributed binaries")
    publisher = observer(record.get("completed"))
    require(held["run"] == "null" or publisher["run"] == held["run"], "binary proof publication run differs from request")
    trusted(publisher)
    seal_raw = release.fetch(f"{authority}/{root}/seal.json")
    seal = json.loads(seal_raw)
    require(seal.get("schema") == 1 and all(seal.get(name) == value for name, value in (("product", identity["repository"].split("/")[1]), ("releaseVersion", identity["marker"]), ("channel", marker.channel(identity["marker"])), ("commit", identity["commit"]))), "binary proof public seal identity differs")
    return {"source": identity, "publisher": publisher, "distribution": record, "seal": seal, "seal_ref": release.remote(authority, root + "/seal.json", seal_raw, release.JSON)}
