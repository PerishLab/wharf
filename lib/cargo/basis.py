from pathlib import Path

from lib.cargo import lock, manifest, toolchain
from lib.process import git
from lib.refusal import Refusal

CONFIG = ".cargo/config.toml"
WIDENED = [
    "members are whole directories, including tests and fixtures",
    "the lock closure follows every locked edge, including dev-dependencies",
    "Cargo.toml is the whole root manifest",
]


DEPENDED = [
    "members are their manifests, because what a dependency compiles to does not follow a member's own sources",
    "the lock closure follows every locked edge, including dev-dependencies",
    "Cargo.toml is the whole root manifest",
]


def reached(source, names):
    root = manifest.read(source, "Cargo.toml")
    workspace = manifest.members(source, root)
    manifest.unversioned(source, root, workspace)
    if not names:
        raise Refusal("a build names at least one executable")
    packages = list(dict.fromkeys(manifest.owner(source, workspace, name) for name in names))
    members = sorted({member for package in packages for member in manifest.closure(source, workspace, package)})
    return workspace, packages, members


def configured(source):
    return {CONFIG: manifest.object_id(source, CONFIG)} if manifest.tracked(source, CONFIG) else {}


def resolve(source, names, target, runner):
    source = Path(source)
    workspace, packages, members = reached(source, names)
    return {
        "entry": {"kind": "cargo-binary", "packages": packages, "binaries": list(names), "target": target, "runner": runner},
        "toolchain": toolchain.current(),
        "manifest": {"Cargo.toml": manifest.object_id(source, "Cargo.toml")},
        "members": {workspace[member]: manifest.object_id(source, workspace[member]) for member in members},
        "lock": lock.closure(source, members),
        "config": configured(source),
        "widened": WIDENED,
    }


def dependencies(source, names, target, runner):
    source = Path(source)
    workspace, packages, members = reached(source, names)
    return {
        "entry": {"kind": "cargo-dependencies", "packages": packages, "binaries": list(names), "target": target, "runner": runner},
        "toolchain": toolchain.current(),
        "manifest": {"Cargo.toml": manifest.object_id(source, "Cargo.toml")},
        "members": {f"{workspace[member]}/Cargo.toml": manifest.object_id(source, f"{workspace[member]}/Cargo.toml") for member in members},
        "lock": lock.closure(source, members),
        "config": configured(source),
        "widened": DEPENDED,
    }


SUITE_WIDENED = [
    "the whole repository tree, because tests read files outside their own crate",
]


def carried(source):
    return manifest.tracked(source, "Cargo.toml")


def suite(source, runner):
    source = Path(source)
    root = manifest.read(source, "Cargo.toml")
    manifest.unversioned(source, root, manifest.members(source, root))
    return {
        "entry": {"kind": "cargo-suite", "scope": "workspace", "runner": runner},
        "toolchain": toolchain.current(),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": SUITE_WIDENED,
    }
