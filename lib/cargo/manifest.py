import fnmatch
import posixpath
import tomllib
from pathlib import Path

from lib.process import git
from lib.refusal import Refusal

KINDS = ("dependencies", "build-dependencies")


def object_id(source, path):
    return git(source, "rev-parse", f"HEAD:{path}")


def tracked(source, path):
    return git(source, "ls-tree", "--name-only", "HEAD", "--", path) == path


def read(source, path):
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
        directories = [path for path in listed if fnmatch.fnmatchcase(path, pattern)] or [pattern]
        for directory in [path for path in directories if tracked(source, f"{path}/Cargo.toml")]:
            found[read(source, f"{directory}/Cargo.toml")["package"]["name"]] = directory
    if not found:
        raise Refusal("the workspace declares no tracked members")
    return found


def binaries(source, directory):
    declared = read(source, f"{directory}/Cargo.toml")
    named = [target["name"] for target in declared.get("bin", [])]
    implicit = declared["package"].get("autobins", True) and tracked(source, f"{directory}/src/main.rs")
    if implicit and declared["package"]["name"] not in named:
        named.append(declared["package"]["name"])
    return named


def owner(source, workspace, name):
    owners = [package for package, directory in workspace.items() if name in binaries(source, directory)]
    if len(owners) != 1:
        raise Refusal(f"binary {name!r} must be declared by exactly one workspace member, found {owners}")
    return owners[0]


def local(workspace, current, spec, base):
    target = posixpath.normpath(posixpath.join(base, spec["path"]))
    matches = [member for member, directory in workspace.items() if posixpath.normpath(directory) == target]
    if not matches:
        raise Refusal(f"{current} depends on path {target} outside the workspace members")
    return matches[0]


def closure(source, workspace, package):
    seen = []
    pending = [package]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.append(current)
        pending.extend(reached(source, workspace, current))
    return sorted(seen)


def reached(source, workspace, current):
    inherited = read(source, "Cargo.toml").get("workspace", {}).get("dependencies", {})
    declared = read(source, f"{workspace[current]}/Cargo.toml")
    found = []
    for kind in KINDS:
        for name, spec in declared.get(kind, {}).items():
            base = workspace[current]
            if isinstance(spec, dict) and spec.get("workspace"):
                spec, base = inherited.get(name, {}), ""
            if isinstance(spec, dict) and "path" in spec:
                found.append(local(workspace, current, spec, base))
    return found


def unversioned(source, root, workspace):
    if root.get("workspace", {}).get("package", {}).get("version", "0.0.0") != "0.0.0":
        raise Refusal("workspace.package.version must be 0.0.0; release identity is bound at distribution")
    for directory in workspace.values():
        version = read(source, f"{directory}/Cargo.toml")["package"].get("version")
        if version not in ({"workspace": True}, "0.0.0"):
            raise Refusal(f"{directory}/Cargo.toml must not declare a version other than 0.0.0")
