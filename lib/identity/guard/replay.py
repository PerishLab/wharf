import json
import tomllib
from pathlib import Path

from lib.cargo import manifest, registry
from lib.content import marker
from lib.refusal import Refusal

INDEX = registry.KNOWN["perish"]["index"]
SOURCE = "sparse+" + INDEX


def applies(held):
    expected = held["request"].expected
    packages = expected.get("packages") if isinstance(expected, dict) else None
    cargo = "cargo" in held["tools"] and (Path(held["root"]) / "Cargo.toml").is_file()
    return cargo and isinstance(packages, list) and all(isinstance(item, dict) and item.get("ecosystem") == "cargo" for item in packages)


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
        found = latest(package["name"], reader or registry.fetch)
        if found != package["version"] and not (package["name"] in members and found == own):
            raise Refusal(f"latest stable {package['name']} moved to {found}; prepare a fresh plan")


def moves(root, expected):
    wanted = {package["name"]: package["version"] for package in expected["packages"]}
    return [(f"{SOURCE}#{name}@{version}", wanted[name]) for name, version in locked(root) if name in wanted and version != wanted[name]]


def resolved(held, execute, git):
    request = held["request"]
    root = held["root"]
    current(root, request.expected, marker.version(request.identity["marker"]))
    for spec, version in moves(root, request.expected):
        execute(held, [held["tools"]["cargo"]["path"], "update", "--package", spec, "--precise", version])
    git(held, "add", "--update")
    return {"context": "lift", "tree": git(held, "write-tree"), "packages": request.expected["packages"]}
