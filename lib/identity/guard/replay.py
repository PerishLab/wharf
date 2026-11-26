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


def applies(held, git):
    expected = held["request"].expected
    packages = expected.get("packages") if isinstance(expected, dict) else None
    if not isinstance(packages, list) or not all(isinstance(item, dict) and item.get("ecosystem") in {"cargo", "npm"} for item in packages):
        return False
    if getattr(held["request"], "material", None) is not None:
        return True
    cargo = "cargo" in held["tools"] and (Path(held["root"]) / "Cargo.toml").is_file()
    return cargo and (not any(item["ecosystem"] == "npm" for item in packages) or "pnpm" in held["tools"] and git(held, "write-tree") == expected.get("tree"))


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


def declared_cargo(root):
    if not manifest.tracked(root, "plumb.toml"):
        return set()
    attachment = manifest.read(root, "plumb.toml").get("release", {}).get("cargo")
    if not isinstance(attachment, dict):
        return set()
    name = attachment.get("registry")
    packages = attachment.get("packages", [])
    if not isinstance(name, str) or not isinstance(packages, list):
        return set()
    workspace = manifest.members(root, manifest.read(root, "Cargo.toml"))
    owned = set()
    for package in packages:
        if package not in workspace:
            continue
        target = manifest.read(root, f"{workspace[package]}/Cargo.toml")["package"].get("publish", True)
        if target is True or (isinstance(target, list) and name in target):
            owned.add(package)
    return owned


def current(root, expected, own, reader=None):
    if not any(package["ecosystem"] == "cargo" for package in expected["packages"]):
        return set()
    owned = declared_cargo(root)
    retained = set()
    for package in expected["packages"]:
        if package["ecosystem"] != "cargo":
            continue
        found = latest(package["name"], reader or registry.fetch)
        if found != package["version"]:
            if package["name"] not in owned or not stable(own) or found != own:
                raise Refusal(f"latest stable {package['name']} moved to {found}; prepare a fresh plan")
            retained.add((package["name"], package["version"]))
    return retained


def moves(root, expected):
    wanted = {package["name"]: package["version"] for package in expected["packages"] if package["ecosystem"] == "cargo"}
    return [(name, version) for name, version in locked(root) if name in wanted and version != wanted[name]]


def removable(section, retained):
    if section[0].strip() != "[[package]]":
        return False
    package = tomllib.loads("".join(section[1:]))
    return package.get("source") == SOURCE and (package["name"], package["version"]) not in retained


def dropped(root, retained=()):
    path = Path(root) / "Cargo.lock"
    sections = []
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith("[") or not sections:
            sections.append([])
        sections[-1].append(line)
    kept = [section for section in sections if not removable(section, retained)]
    path.write_text("".join("".join(section) for section in kept))


def pin(held, execute, retained):
    wanted = dict(retained)
    seen = set()
    while pending := [(name, version) for name, version in moves(held["root"], held["request"].expected) if name in wanted]:
        name, version = pending[0]
        if (name, version) in seen:
            raise Refusal(f"cargo update left {name} {version} unmoved; prepare a fresh plan")
        seen.add((name, version))
        execute(held, [held["tools"]["cargo"]["path"], "update", "--package", f"{SOURCE}#{name}@{version}", "--precise", wanted[name]])


def resolved(held, execute, git):
    request = held["request"]
    root = held["root"]
    own = marker.version(request.identity["marker"])
    retained = current(root, request.expected, own)
    node(held, git)
    restorer = getattr(request, "material", None)
    if restorer is not None:
        restorer(held, git)
        return {"context": "lift", "tree": request.expected["tree"], "packages": request.expected["packages"]}
    if moves(root, request.expected):
        pin(held, execute, retained)
        if moves(root, request.expected):
            dropped(root, retained)
            execute(held, [held["tools"]["cargo"]["path"], "metadata", "--format-version", "1"])
        if pending := moves(root, request.expected):
            name, version = pending[0]
            raise Refusal(f"cargo resolved {name} {version} outside the plan; prepare a fresh plan")
    git(held, "add", "--update")
    tree = git(held, "write-tree")
    if any(item["ecosystem"] == "npm" for item in request.expected["packages"]) and tree != request.expected.get("tree"):
        raise Refusal("mixed snapshot requires the exact planned npm manifests and lockfile; prepare current source")
    return {"context": "lift", "tree": tree, "packages": request.expected["packages"]}


def node(held, git):
    request = held["request"]
    if not any(package["ecosystem"] == "npm" for package in request.expected["packages"]):
        return
    registries = resources.read_json("registries.json")["npm"]
    owned = None
    own = marker.version(request.identity["marker"])
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
        if package["name"] not in owned or not stable(own) or found != own:
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
