import datetime
import json
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath

from lib import parameters
from lib.content import consigned
from lib.media import depot, edit, lineage
from lib.process import git
from lib.refusal import Refusal
from lib.store import r2, yard

TAG_NATIVE = frozenset({"scene", "configuration"})
COMMIT = re.compile(r"[0-9a-f]{40}")


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
    kind = held["kind"]
    if kind == "changelog":
        return consigned.changelog(held["source"], held["marker"], directory)
    validators = {"skill": consigned.skill, "scene": consigned.scene, "configuration": consigned.configuration}
    if kind not in validators:
        raise Refusal(f"unknown depot kind {kind!r}")
    return validators[kind](Path(directory))


def bound(held):
    if not isinstance(held["commit"], str) or not COMMIT.fullmatch(held["commit"]):
        raise Refusal(f"commit {held['commit']!r} is not a 40-char lowercase hex sha")
    release_held = release(held)
    if release_held.commit != held["commit"]:
        raise Refusal(f"marker {held['marker']} resolves to {release_held.commit}, not {held['commit']}")
    head = git(held["source"], "rev-parse", "HEAD")
    tree = git(held["source"], "rev-parse", "HEAD^{tree}")
    if (head, tree) != (release_held.commit, release_held.tree):
        raise Refusal(f"checked-out source {head} does not match immutable tag {held['marker']}")
    return release_held


def material(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or (len(relative) > 1 and relative[1] == ":"):
        raise Refusal(f"material target {relative!r} must be a relative POSIX directory")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or str(posix) != relative or any(part in {".", ".."} for part in posix.parts) or relative == ".":
        raise Refusal(f"material target {relative!r} must be a normalized relative directory")
    anchor = Path(root).resolve()
    current = anchor
    for part in posix.parts:
        current = current / part
        if current.is_symlink():
            raise Refusal(f"material target {relative!r} traverses a symlink")
    if not current.is_dir() or not current.resolve().is_relative_to(anchor):
        raise Refusal(f"material target {relative!r} must resolve to a directory inside source")
    dirty = git(root, "status", "--porcelain", "--untracked-files=all", "--", relative)
    if dirty:
        raise Refusal(f"material target {relative!r} carries changes outside immutable tag checkout")
    return current


def verify_objects(store, folder, content):
    for relative, (entry, body) in sorted(content.items()):
        key = f"{folder}/objects/{relative}"
        if not store.exists(key):
            raise Refusal(f"readback missing lodged object {relative!r}")
        raw = store.get(key)
        if raw != body or depot.sha(raw) != entry["sha256"]:
            raise Refusal(f"readback bytes for {relative!r} differ from tag material")


def readback(store, release_held, kind, expected):
    generation, discovery, content = expected
    channel = depot.channel_of(release_held.marker)
    place = (channel, release_held.marker, kind)
    standing, _ = lineage.pointer(store, place)
    standing = lineage.checked(store, release_held, standing, (channel, kind))
    identity = depot.identity(release_held, channel, kind)
    if standing["version"] != release_held.marker or standing["generation"] != generation or standing["marker"] != identity["marker"]:
        raise Refusal(f"{kind} version standing readback does not match {generation}")
    record = lineage.generation(store, place, generation)
    if record is None or record["document"] != depot.manifest(identity, [entry for entry, _ in content.values()]):
        raise Refusal(f"{kind} manifest readback does not match tag materials")
    verify_objects(store, record["folder"], content)
    floating, _ = lineage.channel_pointer(store, lineage.channel_key(channel, kind), kind)
    floating = lineage.checked(store, release_held, floating, (channel, kind))
    if discovery["state"] == "newer":
        if lineage.order(floating["version"]) <= lineage.order(release_held.marker) or floating["generation"] != discovery["generation"]:
            raise Refusal(f"{kind} channel pointer readback does not match newer standing")
    elif floating["version"] != release_held.marker or floating["generation"] != generation:
        raise Refusal(f"{kind} channel pointer readback does not match {generation}")


def lodge(held):
    release_held = release(held)
    store = r2.writer(depot.LAYOUT["yard"], "DEPOT")
    print(json.dumps(r2.reference(store), sort_keys=True), file=sys.stderr, flush=True)
    document = yard.read(store, held)
    with tempfile.TemporaryDirectory() as scratch:
        directory = yard.unpack(document, scratch)
        verdict = vetted(held, release_held, directory)
        store = bucket(release_held)
        print(json.dumps(r2.reference(store), sort_keys=True), file=sys.stderr, flush=True)
        base = lineage.parent(store, target(release_held, held["kind"]))
        result = edit.stage(store, edit.Change(release_held, held["kind"], edit.directory(directory), base, now()))
        if held["kind"] in lineage.DISCOVERY:
            result["discovery"] = lineage.discovery(store, release_held, held["kind"], result["generation"])
    return dict(result, base=base["generation"] if base else None, vetted=verdict, consigned=held["digest"])


def source(held):
    kind = held["kind"]
    if kind not in TAG_NATIVE:
        raise Refusal(f"tag-native lodging is only supported for scene and configuration, not {kind!r}")
    release_held = bound(held)
    directory = material(held["source"], held["target"])
    verdict = vetted(held, release_held, directory)
    content = edit.directory(directory)
    store = bucket(release_held)
    print(json.dumps(r2.reference(store), sort_keys=True), file=sys.stderr, flush=True)
    base = lineage.parent(store, target(release_held, kind))
    result = edit.stage(store, edit.Change(release_held, kind, content, base, now()))
    result["discovery"] = lineage.discovery(store, release_held, kind, result["generation"])
    readback(store, release_held, kind, (result["generation"], result["discovery"], content))
    return dict(
        result,
        base=base["generation"] if base else None,
        vetted=verdict,
        commit=release_held.commit,
        tree=release_held.tree,
        material=held["target"],
        verified=True,
    )


def rollback(held):
    if held["kind"] != "configuration":
        raise Refusal(f"rollback is only supported for configuration, not {held['kind']!r}")
    release_held = release(held)
    store = bucket(release_held)
    print(json.dumps(r2.reference(store), sort_keys=True), file=sys.stderr, flush=True)
    return lineage.rollback(store, release_held, held["digest"], now())


ACTIONS = {
    "lodge": (lodge, ["repository", "marker", "source", "kind", "digest"]),
    "source": (source, ["repository", "marker", "source", "kind", "commit", "target"]),
    "rollback": (rollback, ["repository", "marker", "source", "kind", "digest"]),
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
