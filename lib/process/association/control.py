import os

from lib.identity.bind import control
from lib.process.association.spec import require

ROLES = {"native-proof": "[prove] native binary", "native-candidate": "[prove] native candidate binary"}


def authority(run):
    repository = control.api("")
    require(repository.get("id") == 1374358737 and repository.get("node_id") == "R_kgDOUesM0Q" and repository.get("default_branch") == "main", "native association repository authority differs")
    require(all(run[name].get("id") == repository["id"] for name in ("repository", "head_repository")), "native association run belongs to another repository")
    branch = control.api("branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "native association needs protected main")


def catalog(identity):
    jobs = control.api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}/jobs?per_page=100")
    rows = jobs.get("jobs")
    require(isinstance(rows, list) and type(jobs.get("total_count")) is int and len(rows) == jobs["total_count"] and 1 <= len(rows) <= 100, "native association job catalog is incomplete")
    require(all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0 and str(row.get("run_id")) == identity["run"] and str(row.get("run_attempt")) == identity["attempt"] and row.get("head_sha") == identity["wharf"] and row.get("head_branch") == "main" for row in rows) and len({row["id"] for row in rows}) == len(rows), "native association job catalog identity differs")
    return rows


def selected(rows, name):
    require(isinstance(rows, list) and 1 <= len(rows) <= 100 and all(isinstance(row, dict) for row in rows), "native declaration role inventory is missing or outside bounds")
    found = [row for row in rows if row.get("name") == name]
    require(len(found) == 1, "native declaration role is missing or ambiguous")
    return found[0]


def active(raw, role):
    identity = control.observer(raw)
    require(role in ROLES and os.environ.get("GITHUB_JOB") == role, "native association requires its fixed native role")
    run = control.trusted(identity, "observer")
    authority(run)
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "native association observer is not active")
    job = selected(catalog(identity), ROLES[role])
    require(job.get("status") == "in_progress" and job.get("conclusion") is None, "native association native role is not active")
    return identity, control.started(job.get("started_at"))


def issued(raw):
    identity = control.observer(raw)
    authority(control.trusted(identity, "issuer"))
    job = selected(catalog(identity), ROLES["native-proof"])
    require(job.get("status") in ("in_progress", "completed"), "native declaration issuer job is not running or settled")
    step = selected(job.get("steps"), ROLES["native-proof"])
    require(step.get("status") == "completed" and step.get("conclusion") == "success", "native declaration proof step did not succeed")
    control.started(step.get("started_at"))
    return identity, control.started(job.get("started_at"))
