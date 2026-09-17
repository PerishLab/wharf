import argparse
import json
import sys
from pathlib import Path

from lib.refusal import Refusal
from lib.store import r2, trigger, workload


def reusable(args):
    return {"key": args.key, "reusable": workload.reusable(r2.configured(), args.key)}


def publish(args):
    produced = workload.Produced(Path(args.dir), json.loads(Path(args.basis).read_text()), json.loads(Path(args.context).read_text()))
    return workload.publish(r2.configured(), args.key, produced)


def fetch(args):
    return workload.fetch(r2.configured(), args.key, args.dir)


def record(args):
    context = json.loads(Path(args.context).read_text())
    needs = json.loads(Path(args.needs).read_text())
    return trigger.record(r2.configured(), context, needs)


def command(actions, name, handler, options):
    parser = actions.add_parser(name)
    for option in options:
        parser.add_argument(f"--{option}", required=True)
    parser.set_defaults(handler=handler)


def parser():
    root = argparse.ArgumentParser(prog="store")
    actions = root.add_subparsers(dest="action", required=True)
    command(actions, "reusable", reusable, ["key"])
    command(actions, "publish", publish, ["key", "dir", "basis", "context"])
    command(actions, "fetch", fetch, ["key", "dir"])
    command(actions, "trigger", record, ["context", "needs"])
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except Refusal as refusal:
        print(f"store: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
