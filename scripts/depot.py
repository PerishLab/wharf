import argparse
import datetime
import json
import sys

from lib.media import depot, edit, lineage
from lib.process import git
from lib.refusal import Refusal
from lib.store import r2


def release(args):
    commit = git(args.source, "rev-parse", f"refs/tags/{args.marker}^{{commit}}")
    tree = git(args.source, "rev-parse", f"refs/tags/{args.marker}^{{tree}}")
    return depot.Release(args.repository, args.marker, commit, tree)


def bucket(held):
    return r2.writer(depot.LAYOUT["bucket"].format(name=held.repository.split("/", 1)[1].lower()), "DEPOT")


def target(held, kind):
    return depot.channel_of(held.marker), held.marker, kind


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(args):
    held = release(args)
    store = bucket(held)
    return edit.pull(store, lineage.base(store, target(held, args.kind), args.from_), args.dir)


def patch(args):
    held = release(args)
    store = bucket(held)
    base = lineage.base(store, target(held, args.kind), args.from_)
    puts = [item.split("=", 1) for item in args.put]
    if any(len(item) != 2 for item in puts):
        raise Refusal("--put takes PATH=FILE")
    content = edit.patched(base, puts, args.remove)
    return dict(edit.stage(store, edit.Change(held, args.kind, content, base, now())), base=base["generation"])


def publish(args):
    held = release(args)
    store = bucket(held)
    if args.full and args.from_:
        raise Refusal("--full and --from are exclusive")
    base = None if args.full else lineage.base(store, target(held, args.kind), args.from_)
    content = edit.directory(args.dir)
    result = edit.stage(store, edit.Change(held, args.kind, content, base, now()))
    return dict(result, base=base["generation"] if base else None)


def compare(args):
    held = release(args)
    store = bucket(held)
    own = lineage.standing(store, target(held, args.kind))
    if not own:
        raise Refusal(f"{held.marker} has no standing {args.kind} generation")
    against = lineage.base(store, target(held, args.kind), args.against) if args.against else lineage.below(store, target(held, args.kind))
    return edit.diff(against, own)


def parser():
    root = argparse.ArgumentParser(prog="depot")
    actions = root.add_subparsers(dest="action", required=True)
    for name, handler in (("pull", pull), ("patch", patch), ("publish", publish), ("diff", compare)):
        command = actions.add_parser(name)
        for option in ("repository", "marker", "source"):
            command.add_argument(f"--{option}", required=True)
        command.add_argument("--kind", required=True, choices=sorted(depot.KINDS))
        command.set_defaults(handler=handler)
        if name != "diff":
            command.add_argument("--from", dest="from_")
        if name in ("pull", "publish"):
            command.add_argument("--dir", required=True)
        if name == "patch":
            command.add_argument("--put", action="append", default=[])
            command.add_argument("--remove", action="append", default=[])
        if name == "publish":
            command.add_argument("--full", action="store_true")
        if name == "diff":
            command.add_argument("--against")
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
