import re
from pathlib import Path

from lib.cargo import manifest
from lib.content import marker as held
from lib.refusal import Refusal

UNVERSIONED = "0.0.0"


def marker(value):
    return held.version(value)


def replace(path, pattern, replacement):
    text = path.read_text()
    changed, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    path.write_text(changed)
    return count


def inject(source, version):
    source = Path(source)
    root = manifest.read(source, "Cargo.toml")
    workspace = manifest.members(source, root)
    manifest.unversioned(source, root, workspace)
    if not replace(source / "Cargo.toml", r'^(version\s*=\s*)"0\.0\.0"', rf'\g<1>"{version}"'):
        raise Refusal("Cargo.toml declares no workspace.package.version to bind")
    for directory in workspace.values():
        replace(source / directory / "Cargo.toml", r'^(version\s*=\s*)"0\.0\.0"', rf'\g<1>"{version}"')
        replace(source / directory / "Cargo.toml", r'(version\s*=\s*)"=0\.0\.0"', rf'\g<1>"={version}"')
    names = "|".join(re.escape(name) for name in workspace)
    replace(source / "Cargo.lock", rf'(name = "(?:{names})"\nversion = )"0\.0\.0"', rf'\g<1>"{version}"')
    touched = [source / "Cargo.toml", source / "Cargo.lock"] + [source / directory / "Cargo.toml" for directory in workspace.values()]
    remaining = [str(path) for path in touched if UNVERSIONED in path.read_text()]
    if remaining:
        raise Refusal(f"{', '.join(remaining)} still mention {UNVERSIONED} after binding {version}")
    return {"version": version, "packages": sorted(workspace)}
