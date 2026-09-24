import hashlib
import json
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.content import canonical, fill, implementation, marker, resources
from lib.media import archive
from lib.refusal import Conflict, Refusal

LAYOUT = resources.read_json("releases.json")
JSON = "application/json; charset=utf-8"
MIMES = {"tar.gz": "application/gzip", "zip": "application/zip"}
MANAGERS = {"unix": ("manage.sh", "text/x-shellscript; charset=utf-8"), "windows": ("manage.ps1", "text/plain; charset=utf-8")}
CANONICAL = "canonical"
PROBE = "exact-v"
TEMPLATES = {"unix": "manager/unix.sh.in", "windows": "manager/windows.ps1.in"}


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


def archived(name, target, spec):
    return f"{name}-{target}.{spec['format']}"


def platforms(name):
    lines = []
    for target, spec in LAYOUT["targets"].items():
        systems = [system for system in spec["systems"] if not system.startswith("Windows:")]
        if systems:
            lines.append(f"    {'|'.join(systems)})\n      ARCHIVE={archived(name, target, spec)}\n      ARTIFACT={spec['key']}\n      ARCHIVE_ROOT=\n      FORMAT={spec['format']}\n      ;;")
    return "\n".join(lines)


def windowed(name):
    for target, spec in LAYOUT["targets"].items():
        if spec["format"] == "zip":
            return {"windows_archive": archived(name, target, spec), "windows_key": spec["key"], "windows_root": ""}
    return None


def named(name, held, channel):
    return {
        "product": name,
        "environment": name.upper().replace("-", "_"),
        "public_url": LAYOUT["authority"].replace("{name}", name),
        "default_channel": channel,
        "default_version": held,
        "binaries": name,
        "version_probe": PROBE,
        "unix_platforms": platforms(name),
        "windows_archive": "",
        "windows_key": "",
        "windows_root": "",
    }


def written(output, platform, body):
    path = Path(output) / MANAGERS[platform][0]
    path.write_text(body)
    if platform == "unix":
        path.chmod(0o755)
    return path


def filled(name, held, marker, output):
    values = named(name, held, channel(marker))
    written(output, "unix", fill.fill(resources.read_bytes(TEMPLATES["unix"]).decode(), values))
    windows = windowed(name)
    if windows is not None:
        written(output, "windows", fill.fill(resources.read_bytes(TEMPLATES["windows"]).decode(), {**values, **windows}))
    return output


def render(release, output):
    output = Path(output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    name = release.repository.split("/", 1)[1]
    output.mkdir(parents=True)
    filled(name, release.marker, release.marker, output)
    if channel(release.marker) == "stable":
        rooted = output / CANONICAL
        rooted.mkdir()
        filled(name, "", release.marker, rooted)
    return {"action": "ship.release.managers", "files": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())}


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


def sealed(release):
    return f"v1/releases/{channel(release.marker)}/{release.marker}/seal.json"


def pointing(held):
    return f"v1/channels/{channel(held)}.json"


def generator(release):
    template = canonical.digest(implementation.resourced(["lib.media.release"], ["releases.json", *TEMPLATES.values()]))
    return {"version": f"wharf {release.wharf[:12]}", "template": template, "origin": {"kind": "source-built", "repository": LAYOUT["writer"], "commit": release.wharf}}


def seal(release, name, held, authority):
    staged, pinned = held
    held = channel(release.marker)
    key = sealed(release)
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
    return pointing(release.marker), json.dumps(document, indent=2).encode()


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


def read(url, reader):
    try:
        return json.loads(reader(url))
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return {}
        raise Refusal(f"release authority answered {failure.code}")


def published(release, reader=fetch):
    name, _, authority = place(release)
    held = read(f"{authority}/{sealed(release)}", reader)
    return held.get("releaseVersion") == release.marker and held.get("product") == name


def carried(source):
    path = Path(source) / "plumb.toml"
    declared = tomllib.loads(path.read_text()).get("release", {}) if path.is_file() else {}
    return bool(declared.get("binaries"))


def presence(source, release, reader=fetch):
    if not carried(source):
        return True, False
    return published(release, reader), True


def overtaken(release, reader=fetch):
    _, _, authority = place(release)
    current = read(f"{authority}/{pointing(release.marker)}", reader).get("releaseVersion", "")
    return marker.holds(current) and order(current) > order(release.marker)


def publish(release, contents, bucket, reader=fetch):
    name, _, authority = place(release)
    staged = objects(name, contents.bound, authority)
    pinned = scripts(contents.managers, authority, False)
    if channel(release.marker) == "stable" and not pinned:
        raise Refusal("a stable release carries its pinned manager scripts")
    key, body = seal(release, name, (staged, pinned), authority)
    if bucket.exists(key):
        if json.loads(bucket.get(key))["artifacts"] != json.loads(body)["artifacts"]:
            raise Refusal(f"{key} already holds different artifacts; a release is immutable")
        return {"seal": key, "state": "already-published"}
    for object_key, object_body, entry in [*staged.values(), *pinned.values()]:
        try:
            bucket.create(object_key, object_body, {"Content-Type": entry["mime"]})
        except Conflict:
            pass
    bucket.create(key, body, {"Content-Type": JSON})
    if hashlib.sha256(reader(f"{authority}/{key}")).hexdigest() != hashlib.sha256(body).hexdigest():
        raise Refusal(f"{authority}/{key} does not serve the written seal")
    return {"seal": key, "state": "published", "artifacts": sorted(staged), "managers": sorted(pinned)}


def point(release, managers, bucket):
    name, _, authority = place(release)
    key = sealed(release)
    if not bucket.exists(key):
        raise Refusal(f"{key} is not written; a channel points only at a published seal")
    body = bucket.get(key)
    held = json.loads(body)
    stable = channel(release.marker) == "stable"
    rooted = scripts(Path(managers) / CANONICAL, authority, True) if stable else {}
    if stable and not (rooted and set(rooted) == set(held["managers"])):
        raise Refusal("a stable channel carries canonical manager scripts for the platforms its seal pins")
    committed = Release(release.repository, release.marker, held["commit"], release.wharf)
    moved = advance(bucket, *pointer(committed, name, remote(authority, key, body, JSON), rooted), release.marker)
    return {"seal": key, "pointer": lead(bucket, rooted, moved, release.marker)}
