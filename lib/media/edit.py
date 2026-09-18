import os
from dataclasses import dataclass
from pathlib import Path

from lib.media import depot, lineage
from lib.refusal import Conflict, Refusal

MEDIA = {".json": "application/json", ".toml": "application/toml", ".yaml": "application/yaml", ".yml": "application/yaml", ".md": "text/plain", ".txt": "text/plain", ".sh": "text/x-shellscript", ".ps1": "text/x-powershell"}


@dataclass(frozen=True)
class Change:
    release: depot.Release
    kind: str
    content: dict
    base: dict
    now: str


def media(path):
    return MEDIA.get(Path(path).suffix, "application/octet-stream")


def local(path, relative):
    body = path.read_bytes()
    entry = {"path": relative, "sha256": depot.sha(body), "size": len(body), "mediaType": media(relative), "executable": bool(path.stat().st_mode & 0o111)}
    return entry, body


def directory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise Refusal(f"{root} is not a plain directory")
    content = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Refusal(f"{path} is a special object")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            content[relative] = local(path, relative)
    if not content:
        raise Refusal(f"{root} holds no objects")
    return content


def inherited(base):
    return {entry["path"]: (entry, None) for entry in base["document"]["objects"]} if base else {}


def patched(base, puts, removes):
    content = inherited(base)
    for relative in removes:
        if relative not in content:
            raise Refusal(f"the base generation holds no {relative} to remove")
        del content[relative]
    for relative, path in puts:
        depot.check(relative)
        content[relative] = local(Path(path), relative)
    return content


def stage(bucket, change):
    held = depot.identity(change.release, depot.channel_of(change.release.marker), change.kind)
    place = (held["channel"], held["version"], held["kind"])
    document = depot.manifest(held, [entry for entry, _ in change.content.values()])
    generation = depot.sha(depot.compact(document))
    current, etag = lineage.pointer(bucket, place)
    if current is not None and current["generation"] == generation:
        return {"generation": generation, "state": "already-published"}
    folder = f"{depot.route(*place)}/generations/{generation}"
    kept = {entry["path"]: entry["sha256"] for entry in change.base["document"]["objects"]} if change.base else {}
    for relative, (entry, body) in sorted(change.content.items()):
        target = f"{folder}/objects/{relative}"
        if body is None or kept.get(relative) == entry["sha256"]:
            bucket.copy(f"{change.base['folder']}/objects/{relative}", target)
        else:
            depot.settle(bucket, target, body, "application/octet-stream")
    depot.settle(bucket, f"{folder}/manifest.json", depot.pretty(document), "application/json")
    written = depot.pointer(document, depot.source(change.release), depot.lineage(current, document), change.now)
    try:
        bucket.swap(f"{depot.route(*place)}/latest.json", depot.pretty(written), etag)
    except Conflict:
        raise Refusal("the pointer moved while this generation was staged; pull again and reapply")
    return {"generation": generation, "previous": written["previousGeneration"], "state": "published"}


def pull(bucket, base, target):
    target = Path(target)
    if target.exists():
        raise Refusal(f"{target} already exists")
    for entry in base["document"]["objects"]:
        body = bucket.get(f"{base['folder']}/objects/{entry['path']}")
        if depot.sha(body) != entry["sha256"]:
            raise Refusal(f"{entry['path']} drifted from its manifest")
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.chmod(path, 0o755 if entry["executable"] else 0o644)
    return {"channel": base["channel"], "version": base["version"], "generation": base["generation"], "objects": len(base["document"]["objects"])}


def diff(left, right):
    before = {entry["path"]: entry for entry in left["document"]["objects"]}
    after = {entry["path"]: entry for entry in right["document"]["objects"]}
    return {
        "from": left["generation"],
        "to": right["generation"],
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
    }
