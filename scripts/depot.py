import datetime
import json
import sys
import tempfile

from lib import parameters
from lib.content import consigned
from lib.media import depot, edit, lineage
from lib.process import git
from lib.refusal import Refusal
from lib.store import r2, yard


def release(held):
    commit = git(held["source"], "rev-parse", f"refs/tags/{held['marker']}^{{commit}}")
    tree = git(held["source"], "rev-parse", f"refs/tags/{held['marker']}^{{tree}}")
    return depot.Release(held["repository"], held["marker"], commit, tree)


def product(release_held):
    return release_held.repository.split("/", 1)[1].lower()


def bucket(release_held):
    return r2.writer(depot.LAYOUT["bucket"].format(name=product(release_held)), "DEPOT")


def target(release_held, kind):
    return depot.channel_of(release_held.marker), release_held.marker, kind


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def vetted(held, release_held, directory):
    if held["kind"] == "changelog":
        return consigned.changelog(held["source"], held["marker"], directory)
    return consigned.skill(held["source"], held["marker"], product(release_held), directory)


def lodge(held):
    release_held = release(held)
    document = yard.read(r2.writer(depot.LAYOUT["yard"], "DEPOT"), held)
    with tempfile.TemporaryDirectory() as scratch:
        directory = yard.unpack(document, scratch)
        verdict = vetted(held, release_held, directory)
        store = bucket(release_held)
        base = lineage.parent(store, target(release_held, held["kind"]))
        result = edit.stage(store, edit.Change(release_held, held["kind"], edit.directory(directory), base, now()))
    return dict(result, base=base["generation"] if base else None, vetted=verdict, consigned=held["digest"])


def compare(held):
    release_held = release(held)
    store = bucket(release_held)
    own = lineage.standing(store, target(release_held, held["kind"]))
    if not own:
        raise Refusal(f"{release_held.marker} has no standing {held['kind']} generation")
    against = lineage.base(store, target(release_held, held["kind"]), held["against"]) if held["against"] else lineage.below(store, target(release_held, held["kind"]))
    return edit.diff(against, own)


ACTIONS = {
    "lodge": (lodge, ["repository", "marker", "source", "kind", "digest"]),
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
