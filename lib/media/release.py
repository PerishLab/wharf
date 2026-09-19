import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib import canonical, implementation, resources
from lib.media import archive
from lib.refusal import Conflict, Refusal

LAYOUT = resources.read_json("releases.json")
JSON = "application/json; charset=utf-8"
MIMES = {"tar.gz": "application/gzip", "zip": "application/zip"}
MARKER = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.([1-9]\d*))?")
STAGES = {"alpha": 0, "beta": 1, "rc": 2}


@dataclass(frozen=True)
class Release:
    repository: str
    marker: str
    commit: str
    wharf: str


def place(release):
    name = release.repository.split("/", 1)[1].lower()
    return name, LAYOUT["bucket"].format(name=name), LAYOUT["authority"].format(name=name)


def channel(marker):
    matched = MARKER.fullmatch(marker)
    if not matched:
        raise Refusal(f"marker {marker!r} is not a release marker")
    return matched.group(4) or "stable"


def order(marker):
    matched = MARKER.fullmatch(marker)
    stage = (1, 0) if matched.group(4) is None else (0, STAGES[matched.group(4)], int(matched.group(5)))
    return tuple(int(part) for part in matched.group(1, 2, 3)) + stage


def remote(authority, key, body, mime):
    name = key.rsplit("/", 1)[1]
    return {"name": name, "mime": mime, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), "url": f"{authority}/{key}"}


def objects(name, bound, authority):
    staged = {}
    for target, spec in sorted(LAYOUT["targets"].items()):
        suffix = ".exe" if "windows" in target else ""
        source = Path(bound[target]) / f"{name}-{target}{suffix}"
        if not source.is_file():
            raise Refusal(f"{source} is missing")
        body = archive.pack(f"{name}{suffix}", source.read_bytes(), spec["format"])
        file = f"{name}-{target}.{spec['format']}"
        key = f"v1/objects/sha256/{hashlib.sha256(body).hexdigest()}/{file}"
        staged[spec["key"]] = (key, body, remote(authority, key, body, MIMES[spec["format"]]))
    return staged


def generator(release):
    template = canonical.digest(implementation.resourced(["lib.media.release"], ["releases.json"]))
    return {"version": f"wharf {release.wharf[:12]}", "template": template, "origin": {"kind": "source-built", "repository": LAYOUT["writer"], "commit": release.wharf}}


def seal(release, name, staged, authority):
    held = channel(release.marker)
    key = f"v1/releases/{held}/{release.marker}/seal.json"
    document = {
        "schema": 1,
        "product": name,
        "channel": held,
        "releaseVersion": release.marker,
        "commit": release.commit,
        "url": f"{authority}/{key}",
        "generator": generator(release),
        "provenance": {"kind": "native"},
        "artifacts": {artifact: entry[2] for artifact, entry in sorted(staged.items())},
        "managers": {},
    }
    return key, json.dumps(document, indent=2).encode()


def pointer(release, name, sealed, authority):
    held = channel(release.marker)
    document = {"schema": 1, "product": name, "channel": held, "releaseVersion": release.marker, "commit": release.commit, "seal": sealed, "managers": {}}
    return f"v1/channels/{held}.json", json.dumps(document, indent=2).encode()


def advance(bucket, key, body, marker):
    if bucket.exists(key):
        current = json.loads(bucket.get(key))["releaseVersion"]
        if MARKER.fullmatch(current) and order(current) >= order(marker):
            return {"pointer": key, "state": "kept", "current": current}
    bucket.put(key, body, {"Content-Type": JSON})
    return {"pointer": key, "state": "advanced"}


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wharf"}), timeout=30) as response:
        return response.read()


def published(release, reader=fetch):
    name, _, authority = place(release)
    try:
        held = json.loads(reader(f"{authority}/v1/releases/{channel(release.marker)}/{release.marker}/seal.json"))
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return False
        raise Refusal(f"release authority answered {failure.code}")
    return held.get("releaseVersion") == release.marker and held.get("product") == name


def publish(release, bound, bucket, reader=fetch):
    if channel(release.marker) == "stable":
        raise Refusal("a stable release carries its manager scripts, which this path does not publish yet")
    name, _, authority = place(release)
    staged = objects(name, bound, authority)
    key, body = seal(release, name, staged, authority)
    if bucket.exists(key):
        held = json.loads(bucket.get(key))
        if held["artifacts"] != json.loads(body)["artifacts"]:
            raise Refusal(f"{key} already holds different artifacts; a release is immutable")
        return {"seal": key, "state": "already-published", "pointer": advance(bucket, *pointer(release, name, remote(authority, key, bucket.get(key), JSON), authority), release.marker)}
    for object_key, object_body, entry in staged.values():
        try:
            bucket.create(object_key, object_body, {"Content-Type": entry["mime"]})
        except Conflict:
            pass
    bucket.create(key, body, {"Content-Type": JSON})
    moved = advance(bucket, *pointer(release, name, remote(authority, key, body, JSON), authority), release.marker)
    if hashlib.sha256(reader(f"{authority}/{key}")).hexdigest() != hashlib.sha256(body).hexdigest():
        raise Refusal(f"{authority}/{key} does not serve the written seal")
    return {"seal": key, "state": "published", "artifacts": sorted(staged), "pointer": moved}
