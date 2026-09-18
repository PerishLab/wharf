import argparse
import datetime
import json
import sys

from lib import resources
from lib.media import depot
from lib.refusal import Refusal
from lib.store import r2

RELEASES = resources.read_json("releases.json")


def publish(args):
    release = depot.Release(args.repository, args.marker, args.commit, args.tree)
    name = args.repository.split("/", 1)[1].lower()
    stable = depot.stable_version(RELEASES["authority"].format(name=name))
    carry = depot.carried(depot.source(release), "stable", stable)
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    bucket = r2.writer(depot.LAYOUT["bucket"].format(name=name), "DEPOT")
    return dict(depot.publish(bucket, depot.Publication(release, carry, now)), stable=stable)


def parser():
    root = argparse.ArgumentParser(prog="depot")
    actions = root.add_subparsers(dest="action", required=True)
    command = actions.add_parser("publish")
    for option in ("repository", "marker", "commit", "tree"):
        command.add_argument(f"--{option}", required=True)
    command.set_defaults(handler=publish)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except Refusal as refusal:
        print(f"depot: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
