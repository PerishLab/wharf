import argparse
import json
import os
import sys
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship import binary, cargo
from wharf.store import trigger, workload
from wharf.store.r2 import Bucket


def bucket():
    names = ["WHARF_R2_ENDPOINT", "WHARF_R2_BUCKET", "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY"]
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise Refusal(f"missing store configuration: {', '.join(missing)}")
    return Bucket(*(os.environ[name] for name in names))


def ship_binary(args):
    return binary.build(args.source, args.name, args.target, args.output)


def ship_key_binary(args):
    inputs = cargo.resolve(args.source, args.name, args.target, args.runner)
    digest = workload.key(inputs, workload.implementation(binary, cargo))
    Path(args.inputs).write_bytes(workload.canonical(inputs))
    return {"key": digest}


def ship_smoke(args):
    return binary.smoke(args.dir, args.name, args.target, args.output)


def ship_key_smoke(args):
    inputs = {"entry": {"kind": "binary-smoke", "binary": args.binary_key}}
    Path(args.inputs).write_bytes(workload.canonical(inputs))
    return {"key": workload.key(inputs, workload.implementation(binary))}


def store_fetch(args):
    return workload.fetch(bucket(), args.key, args.dir)


def store_trigger(args):
    context = json.loads(Path(args.context).read_text())
    needs = json.loads(Path(args.needs).read_text())
    return trigger.record(bucket(), context, needs)


def store_reusable(args):
    return {"key": args.key, "reusable": workload.reusable(bucket(), args.key)}


def store_publish(args):
    inputs = json.loads(Path(args.inputs).read_text())
    context = json.loads(Path(args.context).read_text())
    return workload.publish(bucket(), args.key, args.dir, inputs, context)


def parser():
    root = argparse.ArgumentParser(prog="wharf")
    paths = root.add_subparsers(dest="path", required=True)

    ship = paths.add_parser("ship").add_subparsers(dest="action", required=True)
    build = ship.add_parser("binary", help="build one Cargo binary for one target")
    build.add_argument("--source", required=True)
    build.add_argument("--name", required=True)
    build.add_argument("--target", required=True)
    build.add_argument("--output", required=True)
    build.set_defaults(run=ship_binary)

    keyed = ship.add_parser("key", help="resolve effective inputs and the workload key").add_subparsers(dest="entry", required=True)
    entry = keyed.add_parser("binary")
    entry.add_argument("--source", required=True)
    entry.add_argument("--name", required=True)
    entry.add_argument("--target", required=True)
    entry.add_argument("--runner", required=True)
    entry.add_argument("--inputs", required=True)
    entry.set_defaults(run=ship_key_binary)
    check = keyed.add_parser("smoke")
    check.add_argument("--binary-key", required=True)
    check.add_argument("--inputs", required=True)
    check.set_defaults(run=ship_key_smoke)

    smoke = ship.add_parser("smoke", help="run a fetched binary's version and help surfaces")
    smoke.add_argument("--dir", required=True)
    smoke.add_argument("--name", required=True)
    smoke.add_argument("--target", required=True)
    smoke.add_argument("--output", required=True)
    smoke.set_defaults(run=ship_smoke)

    store = paths.add_parser("store").add_subparsers(dest="action", required=True)
    lookup = store.add_parser("reusable", help="report whether a workload record exists")
    lookup.add_argument("--key", required=True)
    lookup.set_defaults(run=store_reusable)
    record = store.add_parser("publish", help="record produced files under a workload key")
    record.add_argument("--key", required=True)
    record.add_argument("--dir", required=True)
    record.add_argument("--inputs", required=True)
    record.add_argument("--context", required=True)
    record.set_defaults(run=store_publish)
    fetched = store.add_parser("fetch", help="download and verify a recorded workload")
    fetched.add_argument("--key", required=True)
    fetched.add_argument("--dir", required=True)
    fetched.set_defaults(run=store_fetch)
    trail = store.add_parser("trigger", help="write the trigger record of this run")
    trail.add_argument("--context", required=True)
    trail.add_argument("--needs", required=True)
    trail.set_defaults(run=store_trigger)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.run(args)
    except Refusal as refusal:
        print(f"wharf: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
