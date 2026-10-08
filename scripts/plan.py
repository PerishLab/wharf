import json
import sys
from pathlib import Path

from lib import parameters
from lib.cargo import basis, toolchain
from lib.content import executables, implementation, marker, resources
from lib.debian import package
from lib.debian.version import debian
from lib.media import cfworker, node, npm, release
from lib.identity.guard import snapshot
from lib.process import git
from lib.refusal import Refusal
from lib.store import plan, r2, workload

BUILD = resources.read_json("build.json")
UNITS = resources.read_json("units.json")
RUNNER = BUILD["runner"]
PRIMARY = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])
CARGO = (["lib.cargo.basis", "lib.cargo.build"], ["build.json", "identity/format.json"])
SINGLE = ("cfworker",)
REPORTED = ("key", "decision", "presence", "existing")
MEDIA = ("npm", "oci", "chart", "cargo", "release", "channel")
IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = IDENTITY + ("wharf", "run", "attempt")
TAKEN = IDENTITY + ("wharf", "run", "attempt", "source", "steps")
CHECKED = ("repository", "marker")
SOURCED = ("source",)
NAMED = ("repository",)


def referenced(held, keys):
    if isinstance(held, dict):
        return set().union(*(referenced(value, keys) for value in held.values()))
    if isinstance(held, list):
        return set().union(*(referenced(value, keys) for value in held))
    return {keys[held]} if isinstance(held, str) and held in keys else set()


def decided(bucket, held, modules, entries):
    key = workload.key(held, implementation.resourced(*modules))
    consumed = sorted(referenced(held, {entry["key"]: name for name, entry in entries.items() if "key" in entry}))
    return {"key": key, "decision": "skip" if workload.reusable(bucket, key) else "run", **({"consumes": consumed} if consumed else {})}


def declared(source, product):
    union = release.targets(source)
    if PRIMARY["target"] not in union:
        raise Refusal(f"a released binary is validated on {PRIMARY['target']}, which plumb.toml does not declare")
    listed = executables.declared(source, union)
    primary = executables.primary(listed, product)
    for name in (primary, *(executables.placed(source, kind, listed, product) for kind in ("deb", "oci"))):
        if name is not None and name not in executables.built(listed, PRIMARY["target"]):
            raise Refusal(f"{name} is validated or placed on {PRIMARY['target']}, which its targets leave out")
    return listed


def binaries(held, bucket, entries):
    identity = {field: held[field] for field in IDENTITY}
    listed = declared(held["source"], plan.product(identity))
    for target in (target for target in BUILD["targets"] if target["target"] in release.targets(held["source"])):
        known, names = target["name"], executables.built(listed, target["target"])
        if not names:
            raise Refusal(f"plumb.toml declares {target['target']} and no executable built for it")
        entries[f"dependencies-{known}"] = decided(bucket, basis.dependencies(held["source"], names, target["target"], target["runner"]), CARGO, entries)
        entries[f"binary-{known}"] = decided(bucket, basis.resolve(held["source"], names, target["target"], target["runner"]), CARGO, entries)
        bound = {"entry": {"kind": "binary-identity", "binary": entries[f"binary-{known}"]["key"]}, "identity": identity}
        entries[f"bind-{known}"] = decided(bucket, bound, (["lib.identity.bind"], ["identity/format.json"]), entries)
        smoked = {"entry": {"kind": "binary-smoke", "binary": entries[f"bind-{known}"]["key"]}}
        entries[f"smoke-{known}"] = decided(bucket, smoked, (["lib.identity.smoke"], []), entries)


def placements(held, bucket, entries):
    listed = executables.declared(held["source"], release.targets(held["source"]))
    binary = executables.placed(held["source"], "deb", listed, plan.product(held))
    if binary is None:
        return
    bound = entries[f"bind-{PRIMARY['name']}"]["key"]
    built = package.basis(held["source"], package.declared(held["source"], binary), bound, debian(held["marker"]))
    entries["deb"] = decided(bucket, built, (["lib.debian.package"], ["releases.json"]), entries)
    verified = {"entry": {"kind": "deb-verify", "deb": entries["deb"]["key"]}}
    entries["verify-deb"] = decided(bucket, verified, (["lib.debian.verify"], ["build.json"]), entries)


def suites(held, bucket, entries):
    source = held["source"]
    if basis.carried(source):
        entries["suite-linux"] = decided(bucket, basis.suite(source, RUNNER), (["lib.cargo.basis", "lib.cargo.suite"], ["build.json"]), entries)
    if node.carried(source):
        entries["suite-node"] = decided(bucket, node.basis(source, RUNNER), (["lib.media.node"], []), entries)
    if cfworker.workers(source):
        entries["cfworker"] = decided(bucket, cfworker.basis(source, RUNNER), (["lib.media.cfworker"], []), entries)


def media(observed, entries):
    for name in MEDIA:
        decision = observed.get(name, {}).get("decision")
        if decision not in ("run", "skip"):
            raise Refusal(f"step {name} reported no decision for this plan to record")
        entries[name] = {"decision": decision, "presence": observed[name].get("presence", "present")}


def validation(bucket, entries):
    if f"bind-{BUILD['primary']}" not in entries:
        return
    primary = entries[f"bind-{BUILD['primary']}"]["key"]
    held = {"entry": {"kind": "binary-validate", "binary": primary}}
    entries["validate"] = decided(bucket, held, (["lib.identity.smoke"], ["validators.json"]), entries)
    for name in ("validate", "deb", "verify-deb"):
        if name in entries and entries["release"]["decision"] == "skip":
            entries[name]["decision"] = "skip"


def reported(text):
    held = json.loads(text or "{}")
    return {name: {field: value for field, value in (step.get("outputs") or {}).items() if field in REPORTED} for name, step in held.items()}


def unit(name):
    kind, _, target = name.partition("-")
    spec = UNITS["units"].get(name) or UNITS["units"].get(kind)
    if spec is None:
        return None
    named = f"[{spec['verb']}] {spec['object']}"
    if spec.get("per") != "target":
        return {"name": named, "action": spec["action"], "target": "", "runner": RUNNER, "prepare": spec.get("prepare", [])}
    known = next(item for item in BUILD["targets"] if item["name"] == target)
    prepared = UNITS["prepare"].get(target, []) + spec.get("prepare", [])
    return {"name": f"{named} {target}", "action": spec["action"], "target": known["target"], "runner": known["runner"], "prepare": prepared}


def units(entries):
    layers = plan.layered(entries)["layers"]
    if len(layers) > UNITS["layers"]:
        raise Refusal(f"the plan derived {len(layers)} layers, the workflow runs {UNITS['layers']}")
    held = {}
    for number in range(1, UNITS["layers"] + 1):
        found = [unit(name) for name in (layers[number - 1] if number <= len(layers) else []) if entries[name]["decision"] == "run"]
        held[f"layer-{number}"] = list({item["name"]: item for item in found if item}.values())
    return held


def present(entries):
    held = {name: entries[name].get("presence", "present") if name in entries else "none" for name in MEDIA}
    return dict(held, cfworker="present" if "cfworker" in entries else "none")


def emit(entries, attempt, verified=None):
    answered = {name: entries[name]["decision"] if name in entries else "skip" for name in SINGLE}
    answered.update({name: json.dumps(found, separators=(",", ":")) for name, found in units(entries).items()})
    answered["presence"] = json.dumps(present(entries), separators=(",", ":"), sort_keys=True)
    answered["planned"] = attempt
    if verified is not None:
        answered["snapshot"] = json.dumps(verified, separators=(",", ":"), sort_keys=True)
    parameters.answer(answered)
    return answered


def source(held):
    return parameters.answer({field: git(held["source"], "rev-parse", "HEAD" if field == "commit" else "HEAD^{tree}") for field in ("commit", "tree")})


def record(held):
    bucket = r2.configured()
    identity = {field: held[field] for field in IDENTITY}
    request = snapshot.Request(Path(held["source"]), identity, toolchain.versions(), destination=snapshot.destination(held["source"]))
    with snapshot.prepared(request) as (root, verified, confirm):
        plan.matching(bucket, held, verified)
        scoped = dict(held, source=root)
        entries = {}
        observed = reported(held["steps"])
        if release.carried(root):
            binaries(scoped, bucket, entries)
            placements(scoped, bucket, entries)
        suites(scoped, bucket, entries)
        media(observed, entries)
        if entries["npm"]["decision"] == "run":
            entries["pack-npm"] = decided(bucket, npm.basis(root, held["marker"]), (["lib.media.npm"], []), entries)
        validation(bucket, entries)
        plan.legacy(bucket, held, entries, observed)
        engines = node.expected(root) if node.carried(root) else {}
        context = dict({field: held[field] for field in CONTEXT}, snapshot=verified)
        confirm()
        recorded = plan.record(bucket, context, entries, engines)
        return dict(recorded, decided=emit(entries, held["attempt"], verified), entry={name: entries[name]["decision"] for name in sorted(entries)})


def named(held):
    owner, _, name = held["repository"].partition("/")
    return parameters.answer({"owner": owner, "name": name})


def check(held):
    return {"repository": held["repository"], "marker": held["marker"], "channel": marker.channel(held["marker"])}


ACTIONS = {"record": record, "check": check, "source": source, "named": named}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("plan", ACTIONS, given)
        values, origins = parameters.resolve(action, {"record": TAKEN, "check": CHECKED, "source": SOURCED, "named": NAMED}[action], rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = ACTIONS[action](values)
    except Refusal as refusal:
        print(f"plan: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
