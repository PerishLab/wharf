import hashlib
import json
import re
import tomllib
from pathlib import Path

from lib import parameters
from lib.content import canonical, declaration, executables, marker, resources
from lib.content.static import evidence
from lib.identity.bind import control, publication
from lib.identity.guard import mismatch
from lib.media import release
from lib.process import git
from lib.refusal import Refusal
from lib.store import distribution, plan, workload

SCHEMA = "wharf.release.completion/v1"
MEDIA = ("npm", "oci", "chart", "cargo", "release", "channel")
IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = IDENTITY + ("wharf", "run", "attempt")
UNITS = resources.read_json("units.json")["units"]


def require(value, reason):
    if not value:
        raise Refusal("sealed completion: " + reason)


def scope(source, identity):
    document = tomllib.loads((Path(source) / "plumb.toml").read_text())
    held = declaration.release(source)
    require(release.carried(source) and marker.channel(identity["marker"]) == "stable", "only stable binary products are supported")
    require(not any(name in held for name in ("npm", "oci", "chart", "cargo", "cfworker", "deb")), "additional publication media require separate recovery")
    require("windtun" not in document and "seatrial" not in document.get("guard", {}), "Windtun products require separate recovery")
    require(git(source, "rev-parse", "HEAD") == identity["commit"] and git(source, "rev-parse", "HEAD^{tree}") == identity["tree"], "checkout source differs")
    require(not git(source, "status", "--porcelain", "--untracked-files=all"), "checkout is dirty")
    listed = executables.declared(source, release.targets(source))
    keys = {release.LAYOUT["targets"][target]["key"] for target in release.targets(source) if executables.installed(listed, target)}
    if "apk" in held:
        keys.update(release.LAYOUT["placements"][kind]["key"] for kind in ("apk", "apk-index", "apk-key"))
    require(keys, "no installed binary archive is declared")
    return keys


def object_bytes(authority, entry):
    require(isinstance(entry, dict) and set(entry) == {"name", "mime", "size", "sha256", "url"}, "object reference fields differ")
    digest = entry["sha256"]
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) and type(entry["size"]) is int and 0 < entry["size"] <= 268435456, "object digest or size is invalid")
    require(isinstance(entry["name"], str) and re.fullmatch(r"[A-Za-z0-9._-]+", entry["name"]), "object name is invalid")
    require(entry["url"] == f"{authority}/v1/objects/sha256/{digest}/{entry['name']}", "object is outside its exact content address")
    body = release.fetch(entry["url"])
    require(len(body) == entry["size"] and hashlib.sha256(body).hexdigest() == digest, "public object bytes differ")


def sealed(held, expected):
    identity = {name: held[name] for name in IDENTITY}
    keys = scope(held["source"], identity)
    placed = release.Release(identity["repository"], identity["marker"], identity["commit"], held["wharf"])
    _, _, authority = release.place(placed)
    raw = release.fetch(f"{authority}/{release.sealed(placed)}")
    seal = evidence.decode(raw)
    publication.sealed(identity, seal)
    require(set(seal.get("artifacts", {})) == keys, "seal has missing or extra declared artifacts")
    managers = seal.get("managers")
    require(isinstance(managers, dict) and set(managers) == ({"unix", "windows"} if "windows-x64" in keys else {"unix"}), "pinned managers are incomplete")
    require(plan.combination(expected)["source"] == identity, "frozen publication source differs")
    controls = expected["controls"]
    mismatch.qualified({"identity": identity, "plumb": controls["plumb"], "authority": controls["authority"]}, expected["head"], {"tree": expected["tree"], "packages": expected["packages"]}, json.dumps(expected["guard"]))
    for entry in [*seal["artifacts"].values(), *managers.values()]:
        object_bytes(authority, entry)
    return raw, seal, authority


def native(bucket, document):
    context, entries = document["context"], document["entries"]
    identity = control.observer({name: context[name] for name in ("run", "attempt", "wharf")})
    run = control.trusted(identity)
    require(run.get("status") == "completed", "original run is not quiescent")
    jobs = publication.jobs(identity)
    require(all(job.get("status") == "completed" for job in jobs), "original jobs are still active")
    require(publication.selected(jobs, "[plan] entries", "job").get("conclusion") == "success", "original planner did not succeed")
    for name, entry in entries.items():
        if name in plan.PUBLISHED:
            continue
        require(workload.reusable(bucket, entry["key"]), "original verification or build record is missing")
        kind, _, target = name.partition("-")
        unit = UNITS.get(name) or UNITS.get(kind)
        if unit is None:
            require(kind == "dependencies", "original workload role is unsupported")
            continue
        role = f"[{unit['verb']}] {unit['object']}" + (f" {target}" if unit.get("per") == "target" else "")
        if entry.get("decision") == "run":
            require(publication.selected(jobs, role, "job").get("conclusion") == "success", "original native verification did not succeed")


def original(bucket, held, checkpoint):
    identity = {name: held[name] for name in IDENTITY}
    document = plan.referenced(bucket, {"attempt": checkpoint["plan"]["attempt"], **identity}, checkpoint["plan"])
    require(plan.combination(document["context"]["snapshot"]) == plan.combination(held["snapshot"]), "original publication combination differs")
    require(document["context"]["snapshot"]["guard"] == held["snapshot"]["guard"], "original Guard differs")
    require(document["context"].get("trial") is None, "original plan has Windtun obligations")
    require(document["entries"].get("release", {}).get("decision") == "run", "original plan did not publish binaries")
    native(bucket, document)
    return document


def validate(bucket, held, checkpoint):
    require(isinstance(checkpoint, dict) and set(checkpoint) == {"schema", "seal", "plan"} and checkpoint["schema"] == SCHEMA, "checkpoint fields differ")
    require(bucket.get(plan.publication(held)) == canonical.encode(plan.combination(held["snapshot"])), "publication reservation differs")
    document = original(bucket, held, checkpoint)
    raw, seal, authority = sealed(held, held["snapshot"])
    require(release.remote(authority, release.sealed(release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])), raw, release.JSON) == checkpoint["seal"], "public seal changed")
    require(seal.get("generator", {}).get("origin", {}).get("commit") == document["context"]["wharf"], "seal producer differs from original plan")
    return checkpoint


def prepare(held, bucket, expected):
    placed = release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])
    if expected is None or marker.channel(held["marker"]) != "stable":
        return None
    document = tomllib.loads((Path(held["source"]) / "plumb.toml").read_text())
    table = document.get("release", {})
    if not table.get("binaries") or any(name in table for name in ("npm", "oci", "chart", "cargo", "cfworker", "deb")) or "windtun" in document or "seatrial" in document.get("guard", {}):
        return None
    if not release.published(placed, release.fetch):
        return None
    scoped = dict(held, snapshot=expected)
    _, _, authority = release.place(placed)
    record = evidence.decode(release.fetch(f"{authority}/{distribution.location(held['marker'])}"))
    require(all(record.get(name) == held[name] for name in IDENTITY), "distribution source differs")
    previous = control.observer(record.get("attempt"))
    context = {**{name: held[name] for name in IDENTITY}, **previous}
    document = plan.read(bucket, context)
    require(all(document["context"].get(name) == value for name, value in context.items()), "original plan context differs")
    raw = release.fetch(f"{authority}/{release.sealed(placed)}")
    checkpoint = document["context"].get("completion") or {"schema": SCHEMA, "seal": release.remote(authority, release.sealed(placed), raw, release.JSON), "plan": plan.reference(document)}
    validate(bucket, scoped, checkpoint)
    observed = json.loads(held["steps"])
    require(all(observed[name]["outputs"].get("presence") == "none" for name in MEDIA if name not in ("release", "channel")), "another medium is present")
    require(observed["release"]["outputs"].get("decision") == "skip", "binaries were not observed published")
    entries = {name: {"decision": "skip", "presence": "present" if name == "release" else "none"} for name in MEDIA}
    entries["channel"] = {key: observed["channel"]["outputs"][key] for key in ("decision", "presence")}
    context = {name: held[name] for name in CONTEXT} | {"snapshot": expected, "completion": checkpoint}
    return context, entries


def recorded(bucket, context, entries):
    result = plan.record(bucket, context, entries, {})
    parameters.answer({"binaryproof": "false", "trial": "null", "publisher": json.dumps({name: context[name] for name in IDENTITY}), "windtun": "false", "plan-reference": json.dumps(plan.reference(plan.read(bucket, context)))})
    return dict(result, completion=context["completion"])
