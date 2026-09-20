import datetime
import json
import sys

from lib import parameters
from lib.media import depot, edit, lineage
from lib.process import git
from lib.refusal import Refusal
from lib.store import r2


def release(held):
    commit = git(held["source"], "rev-parse", f"refs/tags/{held['marker']}^{{commit}}")
    tree = git(held["source"], "rev-parse", f"refs/tags/{held['marker']}^{{tree}}")
    return depot.Release(held["repository"], held["marker"], commit, tree)


def bucket(release_held):
    return r2.writer(depot.LAYOUT["bucket"].format(name=release_held.repository.split("/", 1)[1].lower()), "DEPOT")


def target(release_held, kind):
    return depot.channel_of(release_held.marker), release_held.marker, kind


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(held):
    release_held = release(held)
    store = bucket(release_held)
    return edit.pull(store, lineage.base(store, target(release_held, held["kind"]), held["from"]), held["dir"])


def patch(held):
    release_held = release(held)
    store = bucket(release_held)
    base = lineage.base(store, target(release_held, held["kind"]), held["from"])
    puts = [item.split("=", 1) for item in held["put"]]
    if any(len(item) != 2 for item in puts):
        raise Refusal("--put takes PATH=FILE")
    content = edit.patched(base, puts, held["remove"])
    return dict(edit.stage(store, edit.Change(release_held, held["kind"], content, base, now())), base=base["generation"])


def publish(held):
    release_held = release(held)
    store = bucket(release_held)
    if held["full"] and held["from"]:
        raise Refusal("--full and --from are exclusive")
    base = None if held["full"] else lineage.base(store, target(release_held, held["kind"]), held["from"])
    content = edit.directory(held["dir"])
    result = edit.stage(store, edit.Change(release_held, held["kind"], content, base, now()))
    return dict(result, base=base["generation"] if base else None)


def compare(held):
    release_held = release(held)
    store = bucket(release_held)
    own = lineage.standing(store, target(release_held, held["kind"]))
    if not own:
        raise Refusal(f"{release_held.marker} has no standing {held['kind']} generation")
    against = lineage.base(store, target(release_held, held["kind"]), held["against"]) if held["against"] else lineage.below(store, target(release_held, held["kind"]))
    return edit.diff(against, own)


ACTIONS = {
    "pull": (pull, ["repository", "marker", "source", "kind", "dir", "from"]),
    "patch": (patch, ["repository", "marker", "source", "kind", "from", "put", "remove"]),
    "publish": (publish, ["repository", "marker", "source", "kind", "dir", "from", "full"]),
    "diff": (compare, ["repository", "marker", "source", "kind", "against"]),
}




def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("depot", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"depot: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
