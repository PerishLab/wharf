"""Resolve the effective inputs of one Cargo binary entry group.

The entry group is derived from the directory convention: a binary declared by a
workspace member. Its effective content is what can change the bytes that entry
produces, recorded item by item so a rebuild can be explained by diffing two
manifests. Where resolution cannot be precise it widens, never narrows, and says
so in the manifest.
"""

import fnmatch
import posixpath
import subprocess
import tomllib
from pathlib import Path

from wharf.refusal import Refusal

KINDS = ("dependencies", "build-dependencies")


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {source}: {result.stderr.strip()}")
    return result.stdout.strip()


def object_id(source, path):
    return git(source, "rev-parse", f"HEAD:{path}")


def tracked(source, path):
    return git(source, "ls-tree", "--name-only", "HEAD", "--", path) == path


def manifest(source, path):
    if not tracked(source, path):
        raise Refusal(f"{path} is not tracked at HEAD")
    return tomllib.loads((Path(source) / path).read_text())


def members(source, root):
    workspace = root.get("workspace")
    if not workspace or not workspace.get("members"):
        raise Refusal("Cargo.toml must declare a [workspace] with explicit members")
    listed = git(source, "ls-tree", "-d", "--name-only", "-r", "HEAD").splitlines()
    found = {}
    for pattern in workspace["members"]:
        for directory in [path for path in listed if fnmatch.fnmatchcase(path, pattern)] or [pattern]:
            if tracked(source, f"{directory}/Cargo.toml"):
                package = manifest(source, f"{directory}/Cargo.toml")["package"]["name"]
                found[package] = directory
    if not found:
        raise Refusal("the workspace declares no tracked members")
    return found


def binaries(source, directory, declared):
    named = [target["name"] for target in declared.get("bin", [])]
    implicit = declared["package"].get("autobins", True) and tracked(source, f"{directory}/src/main.rs")
    if implicit and declared["package"]["name"] not in named:
        named.append(declared["package"]["name"])
    return named


def owner(source, workspace, name):
    owners = [
        package
        for package, directory in workspace.items()
        if name in binaries(source, directory, manifest(source, f"{directory}/Cargo.toml"))
    ]
    if len(owners) != 1:
        raise Refusal(f"binary {name!r} must be declared by exactly one workspace member, found {owners}")
    return owners[0]


def closure(source, workspace, package):
    seen = []
    pending = [package]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.append(current)
        declared = manifest(source, f"{workspace[current]}/Cargo.toml")
        for kind in KINDS:
            for name, spec in declared.get(kind, {}).items():
                if isinstance(spec, dict) and "path" in spec:
                    target = posixpath.normpath(posixpath.join(workspace[current], spec["path"]))
                    local = [member for member, directory in workspace.items() if posixpath.normpath(directory) == target]
                    if not local:
                        raise Refusal(f"{current} depends on path {target} outside the workspace members")
                    pending.append(local[0])
    return sorted(seen)


def locked(source, roots):
    lock = manifest(source, "Cargo.lock")
    packages = lock.get("package", [])
    index = {}
    for entry in packages:
        index.setdefault(entry["name"], []).append(entry)

    def resolve(reference):
        name, *rest = reference.split(" ")
        candidates = index.get(name, [])
        if rest:
            candidates = [entry for entry in candidates if entry["version"] == rest[0]]
        if len(candidates) != 1:
            raise Refusal(f"Cargo.lock cannot resolve {reference!r} unambiguously")
        return candidates[0]

    seen = {}
    pending = [resolve(root) for root in roots]
    while pending:
        entry = pending.pop()
        identity = (entry["name"], entry["version"], entry.get("source", ""))
        if identity in seen:
            continue
        seen[identity] = entry.get("checksum", "")
        pending.extend(resolve(reference) for reference in entry.get("dependencies", []))
    return [[name, version, origin, seen[(name, version, origin)]] for name, version, origin in sorted(seen)]


def toolchain(source):
    path = "rust-toolchain.toml"
    if not tracked(source, path):
        raise Refusal("the product must declare its Rust toolchain in rust-toolchain.toml")
    declared = manifest(source, path).get("toolchain", {})
    channel = declared.get("channel", "")
    parts = channel.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise Refusal(f"rust-toolchain.toml channel {channel!r} must be an exact x.y.z version")
    return {
        "channel": channel,
        "profile": declared.get("profile", "default"),
        "components": sorted(declared.get("components", [])),
        "targets": sorted(declared.get("targets", [])),
    }


def resolve(source, name, target, runner):
    source = Path(source)
    root = manifest(source, "Cargo.toml")
    for directory in members(source, root).values():
        version = manifest(source, f"{directory}/Cargo.toml")["package"].get("version")
        if version not in ({"workspace": True}, "0.0.0"):
            raise Refusal(f"{directory}/Cargo.toml must not declare a version other than 0.0.0")
    if root.get("workspace", {}).get("package", {}).get("version", "0.0.0") != "0.0.0":
        raise Refusal("workspace.package.version must be 0.0.0; release identity is bound at distribution")
    workspace = members(source, root)
    package = owner(source, workspace, name)
    reached = closure(source, workspace, package)
    config = ".cargo/config.toml"
    return {
        "entry": {"kind": "cargo-binary", "package": package, "binary": name, "target": target, "runner": runner},
        "toolchain": toolchain(source),
        "manifest": {"Cargo.toml": object_id(source, "Cargo.toml")},
        "members": {workspace[member]: object_id(source, workspace[member]) for member in reached},
        "lock": locked(source, reached),
        "config": {config: object_id(source, config)} if tracked(source, config) else {},
        "widened": [
            "members are whole directories, including tests and fixtures",
            "the lock closure follows every locked edge, including dev-dependencies",
            "Cargo.toml is the whole root manifest",
        ],
    }
