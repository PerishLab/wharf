import json
import os
import sys

from lib import parameters
from lib.cargo import basis
from lib.content import implementation, resources
from lib.media import cfworker, node
from lib.refusal import Refusal
from lib.store import plan, r2, workload

BUILD = resources.read_json("build.json")
RUNNER = BUILD["runner"]
FAMILIES = ("binary", "bind", "smoke")
OUTPUT = "GITHUB_OUTPUT"
REPORTED = ("key", "decision")
MEDIA = ("npm", "oci", "chart", "cargo", "release")
IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = IDENTITY + ("wharf", "run", "attempt")
TAKEN = IDENTITY + ("wharf", "run", "attempt", "source", "steps")


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
        entries[f"binary-{target['name']}"] = built
        entries[f"bind-{target['name']}"] = bound
        entries[f"smoke-{target['name']}"] = decided(bucket, {"entry": {"kind": "binary-smoke", "binary": bound["key"]}}, (["lib.identity.smoke"], []))


def suites(held, bucket, entries):
    source = held["source"]
    entries["suite-linux"] = decided(bucket, basis.suite(source, RUNNER), (["lib.cargo.basis", "lib.cargo.suite"], []))
    entries["suite-node"] = decided(bucket, node.basis(source, RUNNER), (["lib.media.node"], []))
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


def settled(entries, observed):
    drift = plan.agreed(entries, observed)
    if drift:
        raise Refusal("the recorded plan disagrees with the steps that produced it: " + "; ".join(drift))


def matrices(entries):
    return {family: [target for target in BUILD["targets"] if entries[f"{family}-{target['name']}"]["decision"] == "run"] for family in FAMILIES}


def emit(held):
    path = os.environ.get(OUTPUT)
    if not path:
        raise Refusal(f"{OUTPUT} is not set, so there is nowhere to tell the runner what to build")
    with open(path, "a") as file:
        for family, targets in sorted(held.items()):
            file.write(f"{family}={json.dumps(targets, separators=(',', ':'))}\n")
    return {family: [target["name"] for target in targets] for family, targets in held.items()}


def record(held):
    bucket = r2.configured()
    entries = {}
    observed = reported(held["steps"])
    binaries(held, bucket, entries)
    suites(held, bucket, entries)
    media(observed, entries)
    settled(entries, observed)
    recorded = plan.record(bucket, {field: held[field] for field in CONTEXT}, entries, node.declared(held["source"]))
    return dict(recorded, matrices=emit(matrices(entries)))


ACTIONS = {"record": record}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("plan", ACTIONS, given)
        values, origins = parameters.resolve(action, TAKEN, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = ACTIONS[action](values)
    except Refusal as refusal:
        print(f"plan: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
