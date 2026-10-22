import json
import os
import urllib.error
import urllib.parse
import urllib.request
import tomllib
from pathlib import Path

from lib.cargo import manifest, registry
from lib.content import marker, resources
from lib.content.static import evidence
from lib.refusal import Refusal
from lib.identity.guard.pull import Held

INDEX = registry.KNOWN["perish"]["index"]
SOURCE = "sparse+" + INDEX


def applies(held):
    expected = held["request"].expected
    packages = expected.get("packages") if isinstance(expected, dict) else None
    cargo = "cargo" in held["tools"] and (Path(held["root"]) / "Cargo.toml").is_file()
    return cargo and isinstance(packages, list) and all(isinstance(item, dict) and item.get("ecosystem") in {"cargo", "npm"} for item in packages) and (not any(item["ecosystem"] == "npm" for item in packages) or "pnpm" in held["tools"])


def locked(root):
    path = Path(root) / "Cargo.lock"
    entries = tomllib.loads(path.read_text()).get("package", []) if path.is_file() else []
    return [(item["name"], item["version"]) for item in entries if item.get("source") == SOURCE]


def stable(version):
    return marker.holds("v" + version) and marker.channel("v" + version) == "stable"


def latest(name, reader):
    lines = [json.loads(line) for line in reader(INDEX + registry.entry(name)).splitlines() if line.strip()]
    found = [line["vers"] for line in lines if not line.get("yanked") and stable(line["vers"])]
    if not found:
        raise Refusal(f"cargo index holds no stable {name}")
    return max(found, key=lambda version: marker.order("v" + version))


def current(root, expected, own, reader=None):
    members = set(manifest.members(root, manifest.read(root, "Cargo.toml")))
    for package in expected["packages"]:
        if package["ecosystem"] != "cargo":
            continue
        found = latest(package["name"], reader or registry.fetch)
        if found != package["version"] and not (package["name"] in members and found == own):
            raise Refusal(f"latest stable {package['name']} moved to {found}; prepare a fresh plan")


def moves(root, expected):
    wanted = {package["name"]: package["version"] for package in expected["packages"] if package["ecosystem"] == "cargo"}
    return [(f"{SOURCE}#{name}@{version}", wanted[name]) for name, version in locked(root) if name in wanted and version != wanted[name]]


def resolved(held, execute, git):
    request = held["request"]
    root = held["root"]
    current(root, request.expected, marker.version(request.identity["marker"]))
    node(held, git)
    attempted = set()
    while pending := moves(root, request.expected):
        spec, version = pending[0]
        if spec in attempted:
            raise Refusal(f"cargo update left {spec} unmoved; prepare a fresh plan")
        attempted.add(spec)
        execute(held, [held["tools"]["cargo"]["path"], "update", "--package", spec, "--precise", version])
    git(held, "add", "--update")
    tree = git(held, "write-tree")
    if any(item["ecosystem"] == "npm" for item in request.expected["packages"]) and tree != request.expected.get("tree"):
        raise Refusal("mixed snapshot requires the exact planned npm manifests and lockfile; prepare current source")
    return {"context": "lift", "tree": tree, "packages": request.expected["packages"]}


def node(held, git):
    request = held["request"]
    registries = resources.read_json("registries.json")["npm"]
    owned = None
    for package in request.expected["packages"]:
        if package["ecosystem"] != "npm":
            continue
        location = registries.get(package["name"].split("/", 1)[0])
        if location is None:
            raise Refusal("mixed snapshot names an unsupported npm registry")
        document = metadata(package["name"], location)
        tags = document.get("dist-tags") if isinstance(document, dict) else None
        versions = document.get("versions") if isinstance(document, dict) else None
        found = tags.get("latest") if isinstance(tags, dict) else None
        entry = versions.get(found) if isinstance(versions, dict) and isinstance(found, str) else None
        if not isinstance(found, str) or not stable(found) or document.get("name") != package["name"] or not isinstance(entry, dict) or entry.get("name") != package["name"] or entry.get("version") != found:
            raise Refusal("mixed snapshot needs current stable npm metadata")
        if found == package["version"]:
            continue
        if owned is None:
            declaration = tomllib.loads(git(held, "show", "HEAD:plumb.toml"))
            owned = declaration.get("release", {}).get("npm", {}).get("packages", [])
        if package["name"] not in owned or found != marker.version(request.identity["marker"]):
            raise Refusal(f"latest stable {package['name']} moved; prepare a fresh plan")


def metadata(name, location):
    known = resources.read_json("registries.json")["npm"]
    if known.get(name.split("/", 1)[0]) != location or not location.startswith("https://"):
        raise Refusal("npm metadata refuses an unsupported registry")
    token = os.environ.get("WHARF_PACKAGES_TOKEN", "")
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise Refusal("npm metadata requires its prepared package reader")
    url = location + urllib.parse.quote(name, safe="@")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.npm.install-v1+json", "User-Agent": "wharf-snapshot"}
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Held())
    try:
        with client.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30) as response:
            body = response.read(evidence.LIMIT + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise Refusal(f"npm metadata refused HTTP {status}") from None
    except OSError:
        raise Refusal("npm metadata could not be read") from None
    return evidence.decode(body)
