import json
import sys
from pathlib import Path

from lib import parameters
from lib.cargo import basis, manifest, toolchain
from lib.content import declaration, executables, implementation, marker, resources
from lib.package.debian import package
from lib.package.debian.version import debian
from lib.media import cfworker, node, npm, release
from lib.media.release import completion
from lib.identity.guard import coverage, material, pull, snapshot
from lib.identity.bind import inventory
from lib.process import git
from lib.process.promotion import preflight
from lib.refusal import Refusal
from lib.store import plan, r2, workload
from lib.store.plan import decided
from lib.package.openwrt import operation
from lib.process.trial import declaration as trial

BUILD = resources.read_json("build/config.json")
UNITS = resources.read_json("units.json")
RUNNER = BUILD["runner"]
PRIMARY = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])
CARGO = (["lib.cargo.basis", "lib.cargo.build"], ["build/config.json", "identity/format.json"])
SINGLE = ("cfworker",)
REPORTED = ("key", "decision", "presence", "existing")
MEDIA = ("npm", "oci", "chart", "cargo", "release", "channel")
IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = IDENTITY + ("wharf", "run", "attempt")
TAKEN = IDENTITY + ("wharf", "run", "attempt", "source", "steps", "preparing", "preparation")
CHECKED = ("repository", "marker")
SOURCED = ("source",)
NAMED = ("repository",)


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
    if binary is not None:
        bound = entries[f"bind-{PRIMARY['name']}"]["key"]
        built = package.basis(held["source"], package.declared(held["source"], binary), bound, debian(held["marker"]))
        entries["deb"] = decided(bucket, built, (["lib.package.debian.package"], ["releases.json"]), entries)
        entries["verify-deb"] = decided(bucket, {"entry": {"kind": "deb-verify", "deb": entries["deb"]["key"]}}, (["lib.package.debian.verify"], ["build/config.json"]), entries)
    selected = operation.declared(held["source"], plan.product(held))
    if selected is not None:
        bound = entries["bind-arm64"]["key"]
        built = operation.package.basis(held["source"], selected, bound, operation.version(held["marker"]))
        entries["apk"] = decided(bucket, built, operation.PACK, entries)
        verified = {"entry": {"kind": "apk-verify", "apk": entries["apk"]["key"], "binary": bound}}
        entries["verify-apk"] = decided(bucket, verified, operation.VERIFY, entries)


def suites(held, bucket, entries):
    source = held["source"]
    if basis.carried(source):
        covered = coverage.tested(held.get("snapshot")) and manifest.virtual(source)
        for target in tested(source):
            if target["name"] == BUILD["primary"] and covered:
                continue
            held_basis = basis.suite(source, target["runner"], target["target"])
            entries[f"suite-{target['name']}"] = decided(bucket, held_basis, (["lib.cargo.basis", "lib.cargo.suite"], ["build/config.json"]), entries)
    if node.carried(source):
        entries["suite-node"] = decided(bucket, node.basis(source, RUNNER), (["lib.media.node"], []), entries)
    if cfworker.workers(source):
        entries["cfworker"] = decided(bucket, cfworker.basis(source, RUNNER), (["lib.media.cfworker"], []), entries)


def tested(source):
    if release.carried(source) or "targets" not in declaration.release(source):
        return [PRIMARY]
    selected = release.targets(source)
    if not selected:
        raise Refusal("a library's explicit release.targets must select at least one native test platform")
    return [target for target in BUILD["targets"] if target["target"] in selected]


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
        if name in entries and entries.get("release", {}).get("decision") == "skip":
            entries[name]["decision"] = "skip"


def reported(text):
    held = json.loads(text or "{}")
    return {name: {field: value for field, value in (step.get("outputs") or {}).items() if field in REPORTED} for name, step in held.items()}


def unit(name, signing=True):
    kind, _, target = name.partition("-")
    spec = UNITS["units"].get(name) or UNITS["units"].get(kind)
    if spec is None:
        return None
    prepared = spec.get("prepare", [])
    if spec["action"] == "bind" and not signing:
        prepared = [tool for tool in prepared if tool != "rcodesign"]
    named = f"[{spec['verb']}] {spec['object']}"
    if spec.get("per") != "target":
        return {"name": named, "action": spec["action"], "target": "", "runner": spec.get("runner", RUNNER), "prepare": prepared}
    known = next(item for item in BUILD["targets"] if item["name"] == target)
    prepared = UNITS["prepare"].get(target, []) + prepared
    return {"name": f"{named} {target}", "action": spec["action"], "target": known["target"], "runner": known["runner"], "prepare": prepared}


def units(entries):
    layers = plan.layered(entries)["layers"]
    if len(layers) > UNITS["layers"]:
        raise Refusal(f"the plan derived {len(layers)} layers, the workflow runs {UNITS['layers']}")
    held = {}
    for number in range(1, UNITS["layers"] + 1):
        found = [unit(name, "bind-macos" in entries) for name in (layers[number - 1] if number <= len(layers) else []) if entries[name]["decision"] == "run"]
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
    answered["signer"] = "rcodesign" if "bind-macos" in entries else ""
    if verified is not None:
        answered["snapshot"] = json.dumps(verified, separators=(",", ":"), sort_keys=True)
    parameters.answer(answered)
    return answered


def source(held):
    return parameters.answer({field: git(held["source"], "rev-parse", "HEAD" if field == "commit" else "HEAD^{tree}") for field in ("commit", "tree")})


def pulled(identity, offers):
    try:
        found = pull.offered(identity["repository"], identity["tree"])
    except Refusal as refusal:
        offers["refused"] = str(refusal)
        return []
    offers["sources"] = {digest: source for source, digest, body in found}
    return [body for source, digest, body in found]


def attributed(offers, guard):
    if "refused" in offers:
        return {"refused": offers["refused"]}
    if "sources" not in offers:
        return None
    return offers["sources"].get(guard.get("digest"), {"offered": len(offers["sources"])})


def storage(bucket):
    return r2.reference(bucket)


def record(held):
    source = Path(held["source"])
    if basis.carried(source):
        manifest.unbound(source)
    bucket = r2.configured()
    identity = {field: held[field] for field in IDENTITY}
    preparing = held.get("preparing", False)
    if preparing:
        preflight.planning(held, identity)
        if bucket.exists(plan.publication(held)):
            raise Refusal("preparation has an existing publication reservation")
    recalled, offers = {}, {}
    expected = plan.reserved(bucket, held, recalled)
    completed = None if preparing else completion.prepare(held, bucket, expected)
    if completed is not None:
        context, entries = completed
        recorded = completion.recorded(bucket, context, entries)
        return dict(recorded, storage=storage(bucket), decided=emit(entries, held["attempt"], context["snapshot"]), entry={name: entry["decision"] for name, entry in entries.items()})
    request = snapshot.Request(Path(held["source"]), identity, toolchain.versions(), expected, destination=snapshot.destination(held["source"]), recall=lambda observed: plan.recall(bucket, observed, recalled), pull=lambda: pulled(identity, offers))
    with snapshot.prepared(request) as (root, verified, confirm):
        plan.matching(bucket, held, verified)
        suite = trial.capture(root, identity)
        scoped = dict(held, source=root, snapshot=verified)
        entries = {}
        observed = reported(held["steps"])
        if release.carried(root):
            binaries(scoped, bucket, entries)
            if not preparing:
                placements(scoped, bucket, entries)
        if not preparing:
            suites(scoped, bucket, entries)
            media(observed, entries)
        if entries.get("npm", {}).get("decision") == "run":
            entries["pack-npm"] = decided(bucket, npm.basis(root, held["marker"]), (["lib.media.npm"], []), entries)
        validation(bucket, entries)
        if not preparing:
            plan.legacy(bucket, held, entries, observed)
        engines = node.expected(root) if node.carried(root) else {}
        taken = attributed(offers, verified["guard"])
        context = dict({field: held[field] for field in CONTEXT}, snapshot=verified, **({"recalled": recalled} if recalled else {}), **({"pulled": taken} if taken is not None else {}))
        if "binary-arm64" in entries:
            context["material"] = material.capture(root, verified)
        if suite is not None:
            context["trial"] = suite
        if release.carried(root):
            context["native_declaration"] = inventory.native(root, identity)
            context["binary_declaration"] = inventory.linux(context["native_declaration"]["model"])
        parameters.answer({"binaryproof": str(bool(context.get("binary_declaration", {}).get("installed"))).lower()})
        confirm()
        if preparing:
            preflight.planning(held, identity)
        parameters.answer({"trial": json.dumps(suite, sort_keys=True, separators=(",", ":")), "publisher": json.dumps(identity, sort_keys=True, separators=(",", ":")), "windtun": str(suite is not None and marker.channel(identity["marker"]) in ("alpha", "beta", "rc")).lower()})
        recorded = plan.record(bucket, context, entries, engines)
        parameters.answer({"plan-reference": json.dumps(plan.reference(plan.read(bucket, context)), sort_keys=True, separators=(",", ":"))})
        remembered = False if recalled else plan.remember(bucket, context)
        return dict(recorded, storage=storage(bucket), recalled=recalled or None, pulled=taken, remembered=remembered, decided=emit(entries, held["attempt"], verified), entry={name: entries[name]["decision"] for name in sorted(entries)})


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
