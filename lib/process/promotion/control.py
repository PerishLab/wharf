import os

from lib.identity.bind import control
from lib.identity.bind.spec import require

ROLE = "promotion-prepare"
JOB = "[windtun] prepare promotion"


def active(raw):
    observer = control.observer(raw)
    require(os.environ.get("GITHUB_JOB") == ROLE, "promotion preparation requires its fixed native job")
    run = control.trusted(observer)
    repository = control.api("")
    require(repository.get("id") == 1374358737 and repository.get("full_name") == control.REPOSITORY and repository.get("default_branch") == "main", "promotion preparation repository authority differs")
    require(all(run[name].get("id") == repository["id"] for name in ("repository", "head_repository")), "promotion preparation producer repository differs")
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "promotion preparation observer is not active")
    reply = control.api(f"actions/runs/{observer['run']}/attempts/{observer['attempt']}/jobs?per_page=100")
    jobs = reply.get("jobs")
    require(isinstance(jobs, list) and len(jobs) == reply.get("total_count") and 1 <= len(jobs) <= 100, "promotion job inventory is incomplete")
    identifiers = set()
    for job in jobs:
        require(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0 and job["id"] not in identifiers, "promotion job identity is invalid or duplicated")
        identifiers.add(job["id"])
        require(str(job.get("run_id")) == observer["run"] and str(job.get("run_attempt")) == observer["attempt"] and job.get("head_sha") == observer["wharf"] and job.get("head_branch") == "main", "promotion job belongs to another attempt or source")
        if job.get("name", "").startswith(("[publish]", "[deploy]", "[point]")):
            require(job.get("started_at") is None, "promotion preparation follows a publication start")
    selected = [job for job in jobs if job.get("name") == JOB]
    require(len(selected) == 1 and selected[0].get("status") == "in_progress" and selected[0].get("conclusion") is None, "promotion preparation native role is absent or inactive")
    return observer, control.started(selected[0].get("started_at"))
