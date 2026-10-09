import re
import tomllib
from pathlib import Path

from lib.cargo import manifest
from lib.content import marker as held
from lib.refusal import Refusal

UNVERSIONED = "0.0.0"
LEFTOVER = re.compile(r"(?<![\w.])0\.0\.0(?![\w.])")
SCHEMA = 1


def marker(value):
    return held.version(value)


def replace(path, pattern, replacement):
    text = path.read_text()
    changed, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    path.write_text(changed)
    return count


def provenance(release):
    held.parts(release.marker)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", release.repository):
        raise Refusal(f"release repository {release.repository!r} is not OWNER/REPOSITORY")
    for name in ("commit", "tree"):
        value = getattr(release, name)
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise Refusal(f"release {name} is not a lowercase Git object identity")
    return {"schema": SCHEMA, "repository": release.repository, "marker": release.marker, "commit": release.commit, "tree": release.tree}


def carry(path, release):
    table = tomllib.loads(path.read_text())
    perish = table.get("package", {}).get("metadata", {}).get("perish", {})
    if not isinstance(perish, dict):
        raise Refusal(f"{path} package.metadata.perish must be a table")
    if "release" in perish:
        raise Refusal(f"{path} already declares Wharf-owned release metadata")
    bound = provenance(release)
    lines = ["", "[package.metadata.perish.release]"] + [f'{name} = "{value}"' if isinstance(value, str) else f"{name} = {value}" for name, value in bound.items()]
    path.write_text(path.read_text().rstrip() + "\n" + "\n".join(lines) + "\n")


def read(source, package):
    source = Path(source)
    workspace = manifest.members(source, manifest.read(source, "Cargo.toml"))
    if package not in workspace:
        raise Refusal(f"Cargo workspace has no package {package}")
    table = tomllib.loads((source / workspace[package] / "Cargo.toml").read_text())
    found = table.get("package", {}).get("metadata", {}).get("perish", {}).get("release")
    if not isinstance(found, dict):
        raise Refusal(f"package {package} carries no bound release metadata")
    return found


def inject(source, release):
    source = Path(source)
    version = marker(release.marker)
    root = manifest.read(source, "Cargo.toml")
    workspace = manifest.members(source, root)
    manifest.unversioned(source, root, workspace)
    if not replace(source / "Cargo.toml", r'^(version\s*=\s*)"0\.0\.0"', rf'\g<1>"{version}"'):
        raise Refusal("Cargo.toml declares no workspace.package.version to bind")
    replace(source / "Cargo.toml", r'(version\s*=\s*)"=0\.0\.0"', rf'\g<1>"={version}"')
    for directory in workspace.values():
        replace(source / directory / "Cargo.toml", r'^(version\s*=\s*)"0\.0\.0"', rf'\g<1>"{version}"')
        replace(source / directory / "Cargo.toml", r'(version\s*=\s*)"=0\.0\.0"', rf'\g<1>"={version}"')
        carry(source / directory / "Cargo.toml", release)
    names = "|".join(re.escape(name) for name in workspace)
    replace(source / "Cargo.lock", rf'(name = "(?:{names})"\nversion = )"0\.0\.0"', rf'\g<1>"{version}"')
    touched = [source / "Cargo.toml", source / "Cargo.lock"] + [source / directory / "Cargo.toml" for directory in workspace.values()]
    remaining = [str(path) for path in touched if LEFTOVER.search(path.read_text())]
    if remaining:
        raise Refusal(f"{', '.join(remaining)} still mention {UNVERSIONED} after binding {version}")
    return {"version": version, "packages": sorted(workspace)}
