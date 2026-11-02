import os

from lib.identity.bind import control
from lib.process.association.spec import require

ROLES = {"native-proof": "[prove] native binary", "native-candidate": "[prove] native candidate binary"}


def active(raw, role):
    identity = control.observer(raw)
    require(role in ROLES and os.environ.get("GITHUB_JOB") == role, "native association requires its fixed native role")
    run = control.trusted(identity, "observer")
    repository = control.api("")
    require(repository.get("id") == 1374358737 and repository.get("node_id") == "R_kgDOUesM0Q" and repository.get("default_branch") == "main", "native association repository authority differs")
    require(all(run[name].get("id") == repository["id"] for name in ("repository", "head_repository")), "native association run belongs to another repository")
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "native association observer is not active")
    branch = control.api("branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "native association needs protected main")
    jobs = control.api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}/jobs?per_page=100")
    rows = jobs.get("jobs")
    require(isinstance(rows, list) and type(jobs.get("total_count")) is int and len(rows) == jobs["total_count"] and 1 <= len(rows) <= 100, "native association job catalog is incomplete")
    require(all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0 and str(row.get("run_id")) == identity["run"] and str(row.get("run_attempt")) == identity["attempt"] and row.get("head_sha") == identity["wharf"] and row.get("head_branch") == "main" for row in rows) and len({row["id"] for row in rows}) == len(rows), "native association job catalog identity differs")
    selected = [row for row in rows if row.get("name") == ROLES[role]]
    require(len(selected) == 1 and selected[0].get("status") == "in_progress" and selected[0].get("conclusion") is None, "native association native role is not active")
    return identity, control.started(selected[0].get("started_at"))
