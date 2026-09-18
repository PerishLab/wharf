import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from lib import canonical, resources
from lib.refusal import Conflict, Refusal

FORMAT = 3
KIND = "configuration"
DIRECTORY = "configurations"
LAYOUT = resources.read_json("depot.json")
DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Release:
    repository: str
    marker: str
    commit: str
    tree: str


@dataclass(frozen=True)
class Publication:
    release: Release
    carry: tuple
    now: str


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


def route(channel, version):
    return f"channels/{channel}/{DIRECTORY}/versions/{version}"


def identity(release, channel):
    marker = {"name": release.marker, "sha256": canonical.digest({"repository": release.repository, "marker": release.marker, "commit": release.commit, "tree": release.tree})}
    return {"product": release.repository.split("/", 1)[1].lower(), "channel": channel, "version": release.marker, "marker": marker, "kind": KIND}


def manifest(held, objects):
    ordered = sorted(objects, key=lambda item: item["path"])
    if not ordered:
        raise Refusal("a depot generation needs objects")
    return {"format": FORMAT, "product": held["product"], "channel": held["channel"], "version": held["version"], "marker": held["marker"], "kind": held["kind"], "objects": ordered}


def pointer(document, base, prior, created):
    generation = sha(compact(document))
    body = pretty(document)
    reference = {"url": f"{base}/{route(document['channel'], document['version'])}/generations/{generation}/manifest.json", "sha256": sha(body), "size": len(body)}
    held = {key: document[key] for key in ("format", "product", "channel", "version", "marker", "kind")}
    return dict(held, generation=generation, manifest=reference, previousGeneration=prior, createdAt=created)


def carried(base, channel, version, reader=fetch):
    standing = reader(f"{base}/{route(channel, version)}/latest.json")
    if standing is None:
        raise Refusal(f"depot carries no {KIND} {version} on {channel} to carry forward")
    held = json.loads(standing)
    document = reader(held["manifest"]["url"])
    if document is None or sha(document) != held["manifest"]["sha256"]:
        raise Refusal(f"depot {KIND} {version} does not bind its manifest")
    objects = json.loads(document)["objects"]
    prefix = held["manifest"]["url"].removesuffix("manifest.json")
    bodies = {}
    for entry in objects:
        body = reader(f"{prefix}objects/{entry['path']}")
        if body is None or sha(body) != entry["sha256"] or len(body) != entry["size"]:
            raise Refusal(f"depot object {entry['path']} drifted from its manifest")
        bodies[entry["path"]] = body
    return objects, bodies, held["generation"]


def standing(bucket, key):
    return json.loads(bucket.get(key)) if bucket.exists(key) else None


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


def publish(bucket, publication, reader=fetch):
    release, now = publication.release, publication.now
    base = source(release)
    objects, bodies, origin = publication.carry
    document = manifest(identity(release, channel_of(release.marker)), objects)
    key = f"{route(document['channel'], document['version'])}/latest.json"
    current = standing(bucket, key)
    generation = sha(compact(document))
    if current is not None and current["generation"] == generation:
        return {"pointer": key, "generation": generation, "state": "already-published"}
    held = pointer(document, base, lineage(current, document), now)
    folder = f"{route(document['channel'], document['version'])}/generations/{generation}"
    for entry in document["objects"]:
        settle(bucket, f"{folder}/objects/{entry['path']}", bodies[entry["path"]], "application/octet-stream")
    settle(bucket, f"{folder}/manifest.json", pretty(document), "application/json")
    body = pretty(held)
    bucket.put(key, body, {"Content-Type": "application/json"})
    if reader(f"{base}/{key}") != body:
        raise Refusal(f"{base}/{key} does not serve the written pointer")
    return {"pointer": key, "generation": generation, "previous": held["previousGeneration"], "carried": origin, "state": "published"}


def channel_of(marker):
    matched = re.fullmatch(r"v\d+\.\d+\.\d+-(alpha|beta|rc)\.[1-9]\d*", marker)
    if not matched:
        raise Refusal(f"marker {marker!r} is not a prerelease; stable configuration is not written here")
    return matched.group(1)


def stable_version(authority, reader=fetch):
    held = reader(f"{authority}/v1/channels/stable.json")
    if held is None:
        raise Refusal(f"{authority} names no stable release to carry configuration from")
    return json.loads(held)["releaseVersion"]
