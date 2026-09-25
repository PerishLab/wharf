import fnmatch
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.content import declaration, resources
from lib.media.node import manifest
from lib.process import git, run
from lib.refusal import Refusal

KNOWN = resources.read_json("registries.json")["npm"]
TOKEN = "NODE_AUTH_TOKEN"
WORKSPACE = "pnpm-workspace.yaml"
UNVERSIONED = "0.0.0"


@dataclass(frozen=True)
class Tools:
    run: object = run
    reader: object = None


def declared(source):
    return declaration.release(source).get("npm")


def carried(source):
    return declared(source) is not None


def globs(source):
    if git(source, "ls-tree", "--name-only", "HEAD", "--", WORKSPACE) != WORKSPACE:
        raise Refusal(f"{WORKSPACE} is not tracked at HEAD")
    lines = (Path(source) / WORKSPACE).read_text().splitlines()
    if "packages:" not in lines:
        raise Refusal(f"{WORKSPACE} must list its packages in block style")
    found = []
    for line in lines[lines.index("packages:") + 1:]:
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+[\"']?([^\"'#\s]+)[\"']?\s*", line)
        if not item:
            break
        found.append(item.group(1))
    if not found:
        raise Refusal(f"{WORKSPACE} lists no packages")
    return found


def publishable(source):
    attachment = declared(source)
    if attachment is None:
        return []
    names = attachment.get("packages", [])
    found = {}
    for path in workspace(source):
        held = manifest(source, path)
        if held.get("name") not in names:
            continue
        if held.get("private"):
            raise Refusal(f"{held['name']} is declared in [release.npm] and {path} marks it private")
        if held.get("version") != UNVERSIONED:
            raise Refusal(f"{path} must declare version {UNVERSIONED}")
        location = registry(source, held["name"])
        if location != attachment.get("registry"):
            raise Refusal(f"{held['name']} maps to {location!r}, [release.npm] declares {attachment.get('registry')!r}")
        found[held["name"]] = {"name": held["name"], "path": path, "registry": location}
    missing = [name for name in names if name not in found]
    if missing:
        raise Refusal(f"[release.npm] declares {', '.join(missing)}, which no workspace package names")
    return [found[name] for name in names]


def workspace(source):
    patterns = globs(source)
    listed = git(source, "ls-files", "*/package.json").splitlines()
    return [path for path in sorted(listed) if any(fnmatch.fnmatchcase(posix_parent(path), pattern) for pattern in patterns)]


def posix_parent(path):
    return path.rsplit("/", 1)[0]


def registry(source, name):
    scope = name.split("/", 1)[0] if name.startswith("@") else ""
    npmrc = Path(source) / ".npmrc"
    mapped = dict(re.findall(r"^(@[\w.-]+):registry=(\S+)\s*$", npmrc.read_text(), re.M)) if npmrc.is_file() else {}
    location = mapped.get(scope, "")
    if not scope or KNOWN.get(scope) != location:
        raise Refusal(f"npm package {name!r} maps to {location!r}, not a known distribution registry")
    return location


def fetch(url):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ.get(TOKEN, '')}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return {}
        raise Refusal(f"npm registry {url} answered {failure.code}")


def published(package, version, reader=fetch):
    url = package["registry"] + urllib.parse.quote(package["name"], safe="@")
    return version in reader(url).get("versions", {})


def pending(source, version, reader=fetch):
    return [dict(package, published=published(package, version, reader)) for package in publishable(source)]


def channel(version):
    matched = re.fullmatch(r"\d+\.\d+\.\d+(?:-(alpha|beta|rc)\.\d+)?", version)
    if not matched:
        raise Refusal(f"version {version!r} is not a release version")
    return matched.group(1) or "latest"


def stamp(source, package, version):
    path = Path(source) / package["path"]
    declared = json.loads(path.read_text())
    declared["version"] = version
    path.write_text(json.dumps(declared, indent="\t") + "\n")


def userconfig(directory, registries):
    if not os.environ.get(TOKEN):
        raise Refusal(f"{TOKEN} is required to publish npm packages")
    path = Path(directory) / "npmrc"
    lines = [f"//{urllib.parse.urlsplit(location).netloc}/:_authToken=${{{TOKEN}}}" for location in sorted(registries)]
    path.write_text("\n".join(lines) + "\n")
    return path


def publish(source, version, tools=Tools()):
    reader = tools.reader or fetch
    held = pending(source, version, reader)
    tag = channel(version)
    results = []
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, NPM_CONFIG_USERCONFIG=str(userconfig(directory, {item["registry"] for item in held})))
        if not all(item["published"] for item in held):
            tools.run(["pnpm", "install", "--frozen-lockfile"], source, env)
        for package in held:
            if package["published"]:
                results.append({"name": package["name"], "state": "already-published"})
                continue
            stamp(source, package, version)
            argv = ["pnpm", "publish", "--filter", package["name"], "--no-git-checks", "--tag", tag]
            tools.run(argv, source, env)
            if not published(package, version, reader):
                raise Refusal(f"{package['name']}@{version} is not visible after publishing")
            results.append({"name": package["name"], "state": "published", "tag": tag})
    return {"version": version, "packages": results}
