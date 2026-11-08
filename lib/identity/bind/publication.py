import os
import subprocess

from lib.content import canonical, marker
from lib.identity.bind import control
from lib.identity.bind.spec import FIELDS, require
from lib.process import git
from lib.refusal import Refusal
from lib.store import plan, r2


def jobs(identity):
    result = control.api(f"actions/runs/{identity['run']}/attempts/{identity['attempt']}/jobs?per_page=100")
    held = result.get("jobs")
    require(isinstance(held, list) and type(result.get("total_count")) is int and len(held) == result["total_count"] and 1 <= len(held) <= 100, "channel job inventory is incomplete")
    require(all(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0 and str(job.get("run_id")) == identity["run"] and str(job.get("run_attempt")) == identity["attempt"] and job.get("head_sha") == identity["wharf"] and job.get("head_branch") == "main" for job in held), "channel jobs belong to another source or attempt")
    require(len({job["id"] for job in held}) == len(held), "channel jobs have duplicate identities")
    return held


COLLECTIONS = {"job": "job inventory", "step": "plan steps"}


def selected(held, name, role):
    found = [item for item in held if item.get("name") == name]
    require(len(found) == 1, f"channel native role is missing or ambiguous: {role}-role {name!r} matched {len(found)} of {len(held)} in {COLLECTIONS[role]}")
    return found[0]


def active(identity):
    require(os.environ.get("GITHUB_JOB") == "channel", "channel requires its fixed native job")
    run = control.trusted(identity)
    repository = control.api("")
    require(repository.get("id") == 1374358737 and repository.get("full_name") == control.REPOSITORY and repository.get("default_branch") == "main" and repository.get("owner", {}).get("id") == 330236468, "channel repository authority differs")
    require(all(run[name].get("id") == repository["id"] for name in ("repository", "head_repository")), "channel producer repository differs")
    branch = control.api("branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "channel requires protected main")
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "channel observer is not active")
    held = jobs(identity)
    job = selected(held, "[point] channel", "job")
    require(job.get("status") == "in_progress" and job.get("conclusion") is None, "channel native role is not active")
    control.started(job.get("started_at"))
    return held


def planned(held):
    identity = control.observer({name: held[name] for name in ("run", "attempt", "wharf")})
    current = active(identity)
    original = control.observer(dict(identity, attempt=held["planned"]))
    require(int(original["attempt"]) <= int(identity["attempt"]), "channel plan attempt is in the future")
    if original != identity:
        control.trusted(original)
        current = jobs(original)
    job = selected(current, "[plan] entries", "job")
    require(job.get("status") == "completed" and job.get("conclusion") == "success", "channel plan job did not succeed")
    steps = job.get("steps")
    require(isinstance(steps, list), "channel plan steps are missing")
    recorded = selected(steps, "[record] plan", "step")
    require(recorded.get("status") == "completed" and recorded.get("conclusion") == "success", "channel plan was not recorded")
    return {name: held[name] for name in ("repository", "marker", "wharf", "run")} | {"attempt": original["attempt"]}


def read(held, continuation=None):
    try:
        query = planned(held)
        bucket = r2.configured()
        document = plan.read(bucket, query)
        require(type(document.get("schema")) is int and document["schema"] == 1, "channel plan schema differs")
        context = document["context"]
        require(isinstance(context, dict) and all(context.get(name) == value for name, value in query.items()), "channel plan context differs")
        plan.planned(document, "channel")
        source = control.source({name: context[name] for name in FIELDS})
        frozen = plan.combination(context["snapshot"])
        require(frozen["source"] == source, "channel snapshot source differs")
        require(git(held["source"], "rev-parse", "HEAD") == source["commit"] and git(held["source"], "rev-parse", "HEAD^{tree}") == source["tree"], "channel checkout differs from planned source")
        require(not git(held["source"], "status", "--porcelain", "--untracked-files=all"), "channel checkout is dirty")
        require(bucket.get(plan.publication(context)) == canonical.encode(frozen), "channel publication reservation differs")
        if "completion" in context:
            require(continuation is not None, "channel requires sealed completion revalidation")
            continuation(bucket, dict(context, source=held["source"]), context["completion"])
        return source
    except Refusal:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OSError, subprocess.SubprocessError) as error:
        raise Refusal(f"channel publication context is unavailable or malformed: {error}") from error


def sealed(source, seal):
    require(isinstance(seal, dict) and type(seal.get("schema")) is int and seal["schema"] == 1, "channel seal schema differs")
    require(all(seal.get(name) == value for name, value in (("product", source["repository"].split("/")[1]), ("releaseVersion", source["marker"]), ("channel", marker.channel(source["marker"])), ("commit", source["commit"]))), "channel seal identity differs from planned source")
