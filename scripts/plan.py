import argparse
import json
import os
import sys

from lib import implementation
from lib.cargo import basis, publish, version
from lib.media import cfworker, chart, node, npm, oci, release as releasing
from lib.refusal import Refusal
from lib.store import plan, r2, workload

TARGETS = (
    ("linux", "x86_64-unknown-linux-gnu", "ubuntu-24.04"),
    ("windows", "x86_64-pc-windows-msvc", "windows-2025"),
    ("macos", "aarch64-apple-darwin", "macos-15"),
)
RUNNER = "ubuntu-24.04"
REPORTED = ("key", "decision")


def decided(bucket, held, modules):
    key = workload.key(held, implementation.resourced(*modules))
    return {"key": key, "decision": "skip" if workload.reusable(bucket, key) else "run"}


def carried(published):
    return {"decision": "skip" if published else "run"}


def binaries(args, bucket, entries):
    identity = {field: getattr(args, field) for field in ("repository", "marker", "commit", "tree")}
    for name, target, runner in TARGETS:
        built = decided(bucket, basis.resolve(args.source, args.name, target, runner), (["lib.cargo.basis", "lib.cargo.build"], []))
        bound = decided(
            bucket,
            {"entry": {"kind": "binary-identity", "binary": built["key"]}, "identity": identity},
            (["lib.identity.bind"], ["identity/format.json"]),
        )
        entries[f"binary-{name}"] = built
        entries[f"bind-{name}"] = bound
        entries[f"smoke-{name}"] = decided(bucket, {"entry": {"kind": "binary-smoke", "binary": bound["key"]}}, (["lib.identity.smoke"], []))


def suites(args, bucket, entries):
    entries["suite-linux"] = decided(bucket, basis.suite(args.source, RUNNER), (["lib.cargo.basis", "lib.cargo.suite"], []))
    entries["suite-node"] = decided(bucket, node.basis(args.source, RUNNER), (["lib.media.node"], []))
    entries["cfworker"] = decided(bucket, cfworker.basis(args.source, RUNNER), (["lib.media.cfworker"], []))


def media(args, entries):
    held = version.marker(args.marker)
    owner = args.repository.split("/", 1)[0]
    entries["npm"] = carried(all(item["published"] for item in npm.pending(args.source, held)))
    entries["oci"] = carried(oci.exists(oci.reference(args.repository, held)))
    entries["chart"] = carried(all(item["published"] for item in chart.pending(args.source, owner, held)))
    entries["cargo"] = carried(all(item["published"] for item in publish.pending(args.source, held)))
    entries["release"] = carried(releasing.published(releasing.Release(args.repository, args.marker, args.commit, args.wharf)))


def reported(text):
    held = json.loads(text or "{}")
    return {name: {field: value for field, value in (step.get("outputs") or {}).items() if field in REPORTED} for name, step in held.items()}


def settled(entries, observed):
    drift = plan.agreed(entries, observed)
    if drift:
        raise Refusal("the recorded plan disagrees with the steps that produced it: " + "; ".join(drift))


def record(args):
    bucket = r2.configured()
    entries = {}
    binaries(args, bucket, entries)
    suites(args, bucket, entries)
    media(args, entries)
    observed = reported(os.environ.get(args.steps, ""))
    if observed:
        settled(entries, observed)
    context = {field: getattr(args, field) for field in ("repository", "marker", "commit", "tree", "wharf", "run", "attempt")}
    held = plan.record(bucket, context, entries, node.declared(args.source))
    return dict(held, agreed=bool(observed))


def parser():
    root = argparse.ArgumentParser(prog="plan")
    actions = root.add_subparsers(dest="action", required=True)
    command = actions.add_parser("record")
    for option in ("source", "name", "repository", "marker", "commit", "tree", "wharf", "run", "attempt", "steps"):
        command.add_argument(f"--{option}", required=True)
    command.set_defaults(handler=record)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except Refusal as refusal:
        print(f"plan: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
