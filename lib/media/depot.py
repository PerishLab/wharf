import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from lib.content import canonical, marker, resources
from lib.refusal import Conflict, Refusal

FORMAT = 3
KINDS = {"configuration": "configurations", "changelog": "changelogs", "skill": "skills"}
STABLE_ONLY = {"changelog"}
LAYOUT = resources.read_json("depot.json")
DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Release:
    repository: str
    marker: str
    commit: str
    tree: str


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False).encode()


def sha(body):
    return hashlib.sha256(body).hexdigest()


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "wharf"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return None
        raise Refusal(f"{url} answered {failure.code}")


def source(release):
    return LAYOUT["source"].format(name=release.repository.split("/", 1)[1].lower())


def directory(kind):
    if kind not in KINDS:
        raise Refusal(f"{kind!r} is not a depot kind; known: {', '.join(KINDS)}")
    return KINDS[kind]


def route(channel, version, kind):
    return f"channels/{channel}/{directory(kind)}/versions/{version}"


def identity(release, channel, kind):
    if kind in STABLE_ONLY and channel != "stable":
        raise Refusal(f"a {kind} describes a stable release; {release.marker} is on the {channel} channel")
    marker = {"name": release.marker, "sha256": canonical.digest({"repository": release.repository, "marker": release.marker, "commit": release.commit, "tree": release.tree})}
    return {"product": release.repository.split("/", 1)[1].lower(), "channel": channel, "version": release.marker, "marker": marker, "kind": kind}


def check(path):
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise Refusal(f"object path {path!r} is not anchored")


def manifest(held, objects):
    ordered = sorted(objects, key=lambda item: item["path"])
    if not ordered:
        raise Refusal("a depot generation needs objects")
    return {"format": FORMAT, "product": held["product"], "channel": held["channel"], "version": held["version"], "marker": held["marker"], "kind": held["kind"], "objects": ordered}


def pointer(document, base, prior, created):
    generation = sha(compact(document))
    body = pretty(document)
    reference = {"url": f"{base}/{route(document['channel'], document['version'], document['kind'])}/generations/{generation}/manifest.json", "sha256": sha(body), "size": len(body)}
    held = {key: document[key] for key in ("format", "product", "channel", "version", "marker", "kind")}
    return dict(held, generation=generation, manifest=reference, previousGeneration=prior, createdAt=created)


def lineage(current, candidate):
    if current is None:
        return None
    same = all(current[field] == candidate[field] for field in ("product", "channel", "version", "marker", "kind"))
    if not same:
        return None
    return current["generation"]


def settle(bucket, key, body, mime):
    try:
        bucket.create(key, body, {"Content-Type": mime})
    except Conflict:
        if bucket.get(key) != body:
            raise Refusal(f"{key} already holds different bytes")


def channel_of(held):
    return marker.channel(held)

