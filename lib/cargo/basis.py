from pathlib import Path

from lib.cargo import lock, manifest, toolchain
from lib.process import git

CONFIG = ".cargo/config.toml"
WIDENED = [
    "members are whole directories, including tests and fixtures",
    "the lock closure follows every locked edge, including dev-dependencies",
    "Cargo.toml is the whole root manifest",
]


def resolve(source, name, target, runner):
    source = Path(source)
    root = manifest.read(source, "Cargo.toml")
    workspace = manifest.members(source, root)
    manifest.unversioned(source, root, workspace)
    package = manifest.owner(source, workspace, name)
    reached = manifest.closure(source, workspace, package)
    config = {CONFIG: manifest.object_id(source, CONFIG)} if manifest.tracked(source, CONFIG) else {}
    return {
        "entry": {"kind": "cargo-binary", "package": package, "binary": name, "target": target, "runner": runner},
        "toolchain": toolchain.declared(source),
        "manifest": {"Cargo.toml": manifest.object_id(source, "Cargo.toml")},
        "members": {workspace[member]: manifest.object_id(source, workspace[member]) for member in reached},
        "lock": lock.closure(source, reached),
        "config": config,
        "widened": WIDENED,
    }


SUITE_WIDENED = [
    "the whole repository tree, because tests read files outside their own crate",
]


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
