import argparse
import json
import sys
from pathlib import Path

from lib import canonical, implementation
from lib.cargo import basis, build, publish, version
from lib.identity import bind
from lib.refusal import Refusal
from lib.smoke import smoke
from lib.store import workload

RELEASE = ("repository", "marker", "commit", "tree")


def release(args):
    return bind.Release(args.repository, args.marker, args.commit, args.tree)


def keyed(held, modules, args):
    Path(args.basis).write_bytes(canonical.encode(held))
    return {"key": workload.key(held, implementation.resourced(*modules))}


def key_binary(args):
    held = basis.resolve(args.source, args.name, args.target, args.runner)
    return keyed(held, (["lib.cargo.basis", "lib.cargo.build"], []), args)


def key_bind(args):
    identity = {field: getattr(args, field) for field in RELEASE}
    held = {"entry": {"kind": "binary-identity", "binary": args.binary_key}, "identity": identity}
    return keyed(held, (["lib.identity.bind"], ["identity/format.json"]), args)


def key_smoke(args):
    held = {"entry": {"kind": "binary-smoke", "binary": args.binary_key}}
    return keyed(held, (["lib.smoke"], []), args)


def run_binary(args):
    return build.build(build.Build(Path(args.source), args.name, args.target, Path(args.output)))


def run_bind(args):
    return bind.perform(bind.Artifact(Path(args.dir), args.name, args.target), release(args), args.binary_key, args.output)


def run_smoke(args):
    return smoke(bind.Artifact(Path(args.dir), args.name, args.target), args.output, args.expect)


def cargo_plan(args):
    held = publish.pending(args.source, version.marker(args.marker))
    return {"published": all(item["published"] for item in held), "packages": held}


def cargo_publish(args):
    bound = version.inject(args.source, version.marker(args.marker))
    return publish.publish(args.source, bound["version"])


def command(actions, name, handler, options):
    parser = actions.add_parser(name)
    for option in options:
        parser.add_argument(f"--{option}", required=True)
    parser.set_defaults(handler=handler)


def parser():
    root = argparse.ArgumentParser(prog="ship")
    actions = root.add_subparsers(dest="action", required=True)
    command(actions, "key-binary", key_binary, ["source", "name", "target", "runner", "basis"])
    command(actions, "key-bind", key_bind, ["binary-key", *RELEASE, "basis"])
    command(actions, "key-smoke", key_smoke, ["binary-key", "basis"])
    command(actions, "binary", run_binary, ["source", "name", "target", "output"])
    command(actions, "bind", run_bind, ["dir", "name", "target", *RELEASE, "binary-key", "output"])
    command(actions, "smoke", run_smoke, ["dir", "name", "target", "output", "expect"])
    command(actions, "cargo-plan", cargo_plan, ["source", "marker"])
    command(actions, "cargo-publish", cargo_publish, ["source", "marker"])
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except Refusal as refusal:
        print(f"ship: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
