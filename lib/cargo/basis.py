from pathlib import Path

from lib.cargo import lock, manifest, toolchain
from lib.process import git

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


def reached(source, name):
    root = manifest.read(source, "Cargo.toml")
    workspace = manifest.members(source, root)
    manifest.unversioned(source, root, workspace)
    package = manifest.owner(source, workspace, name)
    return workspace, package, manifest.closure(source, workspace, package)


def configured(source):
    return {CONFIG: manifest.object_id(source, CONFIG)} if manifest.tracked(source, CONFIG) else {}


def resolve(source, name, target, runner):
    source = Path(source)
    workspace, package, members = reached(source, name)
    return {
        "entry": {"kind": "cargo-binary", "package": package, "binary": name, "target": target, "runner": runner},
        "toolchain": toolchain.declared(source),
        "manifest": {"Cargo.toml": manifest.object_id(source, "Cargo.toml")},
        "members": {workspace[member]: manifest.object_id(source, workspace[member]) for member in members},
        "lock": lock.closure(source, members),
        "config": configured(source),
        "widened": WIDENED,
    }


def dependencies(source, name, target, runner):
    source = Path(source)
    workspace, package, members = reached(source, name)
    return {
        "entry": {"kind": "cargo-dependencies", "package": package, "binary": name, "target": target, "runner": runner},
        "toolchain": toolchain.declared(source),
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
        "toolchain": toolchain.declared(source),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": SUITE_WIDENED,
    }
