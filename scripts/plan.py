import json
import sys

from lib import parameters
from lib.cargo import basis
from lib.content import implementation, marker, resources
from lib.media import cfworker, node
from lib.process import git
from lib.refusal import Refusal
from lib.store import plan, r2, workload

BUILD = resources.read_json("build.json")
RUNNER = BUILD["runner"]
FAMILIES = ("binary", "smoke")
SINGLE = ("suite-linux", "suite-node", "cfworker")
REPORTED = ("key", "decision")
MEDIA = ("npm", "oci", "chart", "cargo", "release")
IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = IDENTITY + ("wharf", "run", "attempt")
TAKEN = IDENTITY + ("wharf", "run", "attempt", "source", "steps")
CHECKED = ("repository", "marker")
SOURCED = ("source",)


def decided(bucket, held, modules):
    key = workload.key(held, implementation.resourced(*modules))
    return {"key": key, "decision": "skip" if workload.reusable(bucket, key) else "run"}


def binaries(held, bucket, entries):
    identity = {field: held[field] for field in IDENTITY}
    name = plan.product(identity)
    for target in BUILD["targets"]:
        built = decided(bucket, basis.resolve(held["source"], name, target["target"], target["runner"]), (["lib.cargo.basis", "lib.cargo.build"], []))
        bound = decided(
            bucket,
            {"entry": {"kind": "binary-identity", "binary": built["key"]}, "identity": identity},
            (["lib.identity.bind"], ["identity/format.json"]),
        )
        entries[f"dependencies-{target['name']}"] = decided(bucket, basis.dependencies(held["source"], name, target["target"], target["runner"]), (["lib.cargo.basis", "lib.cargo.build"], []))
        entries[f"binary-{target['name']}"] = built
        entries[f"bind-{target['name']}"] = bound
        entries[f"smoke-{target['name']}"] = decided(bucket, {"entry": {"kind": "binary-smoke", "binary": bound["key"]}}, (["lib.identity.smoke"], []))


def suites(held, bucket, entries):
    source = held["source"]
    entries["suite-linux"] = decided(bucket, basis.suite(source, RUNNER), (["lib.cargo.basis", "lib.cargo.suite"], []))
    if node.carried(source):
        entries["suite-node"] = decided(bucket, node.basis(source, RUNNER), (["lib.media.node"], []))
    if cfworker.workers(source):
        entries["cfworker"] = decided(bucket, cfworker.basis(source, RUNNER), (["lib.media.cfworker"], []))


def media(observed, entries):
    for name in MEDIA:
        decision = observed.get(name, {}).get("decision")
        if decision not in ("run", "skip"):
            raise Refusal(f"step {name} reported no decision for this plan to record")
        entries[name] = {"decision": decision}


def reported(text):
    held = json.loads(text or "{}")
    return {name: {field: value for field, value in (step.get("outputs") or {}).items() if field in REPORTED} for name, step in held.items()}


def matrices(entries):
    return {family: [target for target in BUILD["targets"] if entries[f"{family}-{target['name']}"]["decision"] == "run"] for family in FAMILIES}


def binding(entries):
    return "run" if any(entries[f"bind-{target['name']}"]["decision"] == "run" for target in BUILD["targets"]) else "skip"


def emit(held, entries):
    answered = {family: json.dumps(targets, separators=(",", ":")) for family, targets in held.items()}
    answered.update({name: entries[name]["decision"] if name in entries else "skip" for name in SINGLE})
    answered["bind"] = binding(entries)
    parameters.answer(answered)
    return answered


def source(held):
    return parameters.answer({field: git(held["source"], "rev-parse", "HEAD" if field == "commit" else "HEAD^{tree}") for field in ("commit", "tree")})


def record(held):
    bucket = r2.configured()
    entries = {}
    observed = reported(held["steps"])
    binaries(held, bucket, entries)
    suites(held, bucket, entries)
    media(observed, entries)
    engines = node.declared(held["source"]) if node.carried(held["source"]) else {}
    recorded = plan.record(bucket, {field: held[field] for field in CONTEXT}, entries, engines)
    return dict(recorded, decided=emit(matrices(entries), entries), entry={name: entries[name]["decision"] for name in sorted(entries)})


def check(held):
    return {"repository": held["repository"], "marker": held["marker"], "channel": marker.channel(held["marker"])}


ACTIONS = {"record": record, "check": check, "source": source}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("plan", ACTIONS, given)
        values, origins = parameters.resolve(action, {"record": TAKEN, "check": CHECKED, "source": SOURCED}[action], rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = ACTIONS[action](values)
    except Refusal as refusal:
        print(f"plan: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
