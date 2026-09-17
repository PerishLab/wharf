import argparse
import json
import sys

from wharf.refusal import Refusal
from wharf.ship import binary


def parser():
    root = argparse.ArgumentParser(prog="wharf")
    paths = root.add_subparsers(dest="path", required=True)

    ship = paths.add_parser("ship").add_subparsers(dest="action", required=True)
    build = ship.add_parser("binary", help="build one Cargo binary for one target")
    build.add_argument("--source", required=True)
    build.add_argument("--name", required=True)
    build.add_argument("--target", required=True)
    build.add_argument("--toolchain", required=True)
    build.add_argument("--output", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        receipt = binary.build(args.source, args.name, args.target, args.toolchain, args.output)
    except Refusal as refusal:
        print(f"wharf: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
