import json
import re
import tempfile
from pathlib import Path

from lib.content import canonical
from lib.content.static import evidence
from lib.identity.bind import control
from lib.identity.bind.spec import entry, require
from lib.media import release
from lib.process.promotion import preflight, reader
from lib.refusal import Refusal
from lib.store import plan, r2

SCHEMA = "wharf.release.semantics.capture/v1"
LIMIT = 4194304


def decode(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= LIMIT, "semantics material exceeds bounds")
    try:
        return json.loads(raw, object_pairs_hook=evidence.pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refusal("semantics material is not valid JSON") from error


def lineage(commit):
    branch = control.api("branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "semantics capture needs protected main")
    head = branch.get("commit", {}).get("sha")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "semantics observer source is invalid")
    require(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head), "protected main source is invalid")
    compared = control.api(f"compare/{commit}...{head}")
    require(compared.get("base_commit", {}).get("sha") == commit and compared.get("merge_base_commit", {}).get("sha") == commit and compared.get("status") in ("ahead", "identical"), "semantics source is outside protected main history")


def active(held, role="semantics"):
    kinds = {"semantics": "capture", "semantics-issue": "adoption", "semantics-world": "world", "semantics-retain": "retain", "semantics-history": "history"}
    require(role in kinds, "semantics role is unsupported")
    observer, jobs = preflight.active(held["observer"], role)
    selected = preflight.event()
    require(selected["semantics"] and all(selected[name] == held[name] for name in ("repository", "marker")), "semantics capture differs from its native request")
    kind = kinds[role]
    require(preflight.semantics(selected["semantics"])["kind"] == kind, "semantics role differs from actual request kind")
    lineage(observer["wharf"])
    names = {preflight.ROLES["request"], preflight.ROLES[role]}
    for job in jobs:
        require(job.get("name") in names or job.get("started_at") is None or job.get("conclusion") == "skipped", "semantics capture overlaps another executing role")
    selected = [job for job in jobs if job.get("name") == preflight.ROLES[role]]
    return {"run": int(observer["run"]), "attempt": int(observer["attempt"]), "commit": observer["wharf"], "job": selected[0]["id"]}


def published(held):
    _, authority, root = control.location(held["repository"], held["marker"])
    url = f"{authority}/{root}/distribution.json"
    raw = release.fetch(url)
    document = decode(raw)
    require(isinstance(document, dict) and type(document.get("schema")) is int and document.get("schema") == 1 and document.get("state") == "complete", "semantics capture needs complete original distribution")
    source = control.source({name: document.get(name) for name in ("repository", "marker", "commit", "tree")})
    require(all(source[name] == held[name] for name in ("repository", "marker")), "semantics distribution names another subject")
    reference = document.get("plan")
    require(isinstance(reference, dict), "original immutable publication plan is unavailable")
    plan.provenance(reference, reference)
    require(all(reference[name] == source[name] for name in source), "original plan names another published source")
    return document, {"url": url, **entry(raw)}


def original(reference):
    lineage(reference["wharf"])
    observer = {name: reference[name] for name in ("run", "attempt", "wharf")}
    run = control.api(f"actions/runs/{observer['run']}/attempts/{observer['attempt']}")
    require(isinstance(run, dict), "original publication run metadata is absent")
    preflight.native(run, observer)
    require(run.get("status") == "completed", "original publication run is not complete")
    catalog = control.api(f"actions/runs/{observer['run']}/attempts/{observer['attempt']}/jobs?per_page=100")
    jobs = catalog.get("jobs")
    require(isinstance(jobs, list) and type(catalog.get("total_count")) is int and 0 < len(jobs) <= 100 and len(jobs) == catalog.get("total_count"), "original planner jobs are incomplete")
    require(all(isinstance(job, dict) and type(job.get("id")) is int and job["id"] > 0 for job in jobs), "original job identities are invalid")
    require(len({job["id"] for job in jobs}) == len(jobs), "original job identities are duplicated")
    chosen = [job for job in jobs if job.get("name") == preflight.ROLES["plan"]]
    require(len(chosen) == 1 and chosen[0].get("status") == "completed" and chosen[0].get("conclusion") == "success", "original native planner did not complete successfully")
    job = chosen[0]
    require(str(job.get("run_id")) == observer["run"] and str(job.get("run_attempt")) == observer["attempt"] and job.get("head_sha") == observer["wharf"] and job.get("head_branch") == "main", "original planner identity differs")
    return plan.referenced(r2.configured(), reference, reference)


def snapshot(distribution, document):
    context = document.get("context")
    require(isinstance(context, dict), "original plan has no captured context")
    world = context.get("snapshot")
    require(isinstance(world, dict), "original plan has no frozen world; no reconstruction attempted")
    source = {name: distribution[name] for name in ("repository", "marker", "commit", "tree")}
    require(world.get("source") == source, "original world names another published source")
    expected = plan.combination(world)
    resolutions = distribution.get("resolution")
    require(isinstance(resolutions, dict) and resolutions and all(value == expected for value in resolutions.values()), "original world differs from recorded publication combinations")
    raw = canonical.encode(world)
    require(len(raw) <= LIMIT, "original world exceeds its material budget")
    return world, raw


def inspect(world, raw):
    authority = reader.identity()
    with tempfile.TemporaryDirectory(prefix="wharf-semantics-") as directory:
        path = Path(directory) / "world.json"
        path.write_bytes(raw)
        code, report = reader.capture(["release", "inspect", "--input", str(path)])
    require(code == 0 and isinstance(report, dict) and set(report) == {"state", "facts", "publication"} and report.get("state") == "observed" and report.get("publication") is False, "shared world inspection did not observe complete materials")
    facts = report.get("facts")
    require(isinstance(facts, dict) and set(facts) == {"schema", "data", "authentication", "publication"} and facts.get("schema") == "plumb.release.inspection/v1" and facts.get("data") == world, "shared inspection names another original world")
    require(facts.get("authentication") is False and facts.get("publication") is False, "data inspection exceeds its authority")
    require(reader.identity() == authority, "shared world reader changed during capture")
    return authority, report


def produce(held):
    destination = Path(held["source"])
    require(destination.is_absolute() and not destination.exists() and not destination.resolve().is_relative_to(Path.cwd().resolve()), "semantics output needs an absent owned path outside source")
    native = active(held)
    distribution, reference = published(held)
    document = original(distribution["plan"])
    world, raw = snapshot(distribution, document)
    authority, inspection = inspect(world, raw)
    require(published(held) == (distribution, reference), "original publication changed during capture")
    record = {"schema": SCHEMA, "source": world["source"], "original": {"distribution": reference, "plan": distribution["plan"]}, "world": {"name": "world.json", **entry(raw)}, "native": native, "reader": authority, "adoption": False, "activation": False, "publication": False}
    destination.mkdir(parents=True)
    (destination / "world.json").write_bytes(raw)
    (destination / "capture.json").write_bytes(canonical.encode(record))
    (destination / "inspection.json").write_bytes(canonical.encode(inspection))
    return record
