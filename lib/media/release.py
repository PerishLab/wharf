import hashlib
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.content import canonical, implementation, marker, resources
from lib.media import archive
from lib.process import run
from lib.refusal import Conflict, Refusal

LAYOUT = resources.read_json("releases.json")
JSON = "application/json; charset=utf-8"
MIMES = {"tar.gz": "application/gzip", "zip": "application/zip"}
MANAGERS = {"unix": ("manage.sh", "text/x-shellscript; charset=utf-8"), "windows": ("manage.ps1", "text/plain; charset=utf-8")}
CANONICAL = "canonical"


@dataclass(frozen=True)
class Release:
    repository: str
    marker: str
    commit: str
    wharf: str


@dataclass(frozen=True)
class Contents:
    bound: dict
    managers: Path


def place(release):
    name = release.repository.split("/", 1)[1].lower()
    return name, LAYOUT["bucket"].format(name=name), LAYOUT["authority"].format(name=name)


def channel(held):
    return marker.channel(held)


def order(held):
    return marker.order(held)


def remote(authority, key, body, mime):
    name = key.rsplit("/", 1)[-1]
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


def render(release, binary, source, output):
    output = Path(output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    step = resources.read_json("managers.json").get(release.repository)
    if not step:
        output.mkdir(parents=True)
        return {"action": "ship.release.managers", "step": None}
    Path(binary).chmod(0o755)
    argv = [str(Path(binary).resolve()), *(part.replace("{marker}", release.marker).replace("{out}", str(output.resolve())) for part in step)]
    with tempfile.TemporaryDirectory() as home:
        try:
            run(argv, source, {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        except subprocess.CalledProcessError as failure:
            detail = (failure.stderr or failure.stdout or "").strip()[:500]
            raise Refusal(f"{' '.join(step)} exited {failure.returncode}: {detail}")
    return {"action": "ship.release.managers", "step": " ".join(step), "files": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())}


def scripts(directory, authority, rooted):
    staged = {}
    for platform, (file, mime) in MANAGERS.items():
        source = Path(directory) / file
        if not source.is_file():
            continue
        body = source.read_bytes()
        key = file if rooted else f"v1/objects/sha256/{hashlib.sha256(body).hexdigest()}/{file}"
        staged[platform] = (key, body, remote(authority, key, body, mime))
    return staged


def managed(release, managers, authority):
    pinned = scripts(managers, authority, False)
    rooted = scripts(Path(managers) / CANONICAL, authority, True) if channel(release.marker) == "stable" else {}
    if channel(release.marker) == "stable" and not (pinned and set(pinned) == set(rooted)):
        raise Refusal("a stable release carries its pinned and canonical manager scripts for the same platforms")
    return pinned, rooted


def generator(release):
    template = canonical.digest(implementation.resourced(["lib.media.release"], ["releases.json", "managers.json"]))
    return {"version": f"wharf {release.wharf[:12]}", "template": template, "origin": {"kind": "source-built", "repository": LAYOUT["writer"], "commit": release.wharf}}


def seal(release, name, held, authority):
    staged, pinned = held
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
        "managers": {platform: entry[2] for platform, entry in sorted(pinned.items())},
    }
    return key, json.dumps(document, indent=2).encode()


def pointer(release, name, sealed, rooted):
    held = channel(release.marker)
    managers = {platform: entry[2] for platform, entry in sorted(rooted.items())}
    document = {"schema": 1, "product": name, "channel": held, "releaseVersion": release.marker, "commit": release.commit, "seal": sealed, "managers": managers}
    return f"v1/channels/{held}.json", json.dumps(document, indent=2).encode()


def lead(bucket, rooted, moved, marker):
    if not rooted or json.loads(bucket.get(moved["pointer"]))["releaseVersion"] != marker:
        return moved
    for key, body, entry in rooted.values():
        bucket.put(key, body, {"Content-Type": entry["mime"]})
    return {**moved, "managers": sorted(key for key, _, _ in rooted.values())}


def advance(bucket, key, body, held):
    if bucket.exists(key):
        current = json.loads(bucket.get(key))["releaseVersion"]
        if marker.holds(current) and order(current) >= order(held):
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


def publish(release, contents, bucket, reader=fetch):
    name, _, authority = place(release)
    staged = objects(name, contents.bound, authority)
    pinned, rooted = managed(release, contents.managers, authority)
    key, body = seal(release, name, (staged, pinned), authority)
    if bucket.exists(key):
        held = json.loads(bucket.get(key))
        if held["artifacts"] != json.loads(body)["artifacts"]:
            raise Refusal(f"{key} already holds different artifacts; a release is immutable")
        moved = advance(bucket, *pointer(release, name, remote(authority, key, bucket.get(key), JSON), rooted), release.marker)
        return {"seal": key, "state": "already-published", "pointer": lead(bucket, rooted, moved, release.marker)}
    for object_key, object_body, entry in [*staged.values(), *pinned.values()]:
        try:
            bucket.create(object_key, object_body, {"Content-Type": entry["mime"]})
        except Conflict:
            pass
    bucket.create(key, body, {"Content-Type": JSON})
    moved = advance(bucket, *pointer(release, name, remote(authority, key, body, JSON), rooted), release.marker)
    if hashlib.sha256(reader(f"{authority}/{key}")).hexdigest() != hashlib.sha256(body).hexdigest():
        raise Refusal(f"{authority}/{key} does not serve the written seal")
    return {"seal": key, "state": "published", "artifacts": sorted(staged), "managers": sorted(pinned), "pointer": lead(bucket, rooted, moved, release.marker)}
