import fnmatch
import hashlib
import json
import posixpath

from lib.check import dependency
from lib.check.locking import literal, records

FIELDS = {"dependencies", "devDependencies", "optionalDependencies"}
ROOT = {"lockfileVersion", "settings", "importers", "packages", "snapshots"}
SETTINGS = {"autoInstallPeers": True, "excludeLinksFromLockfile": False}


def selected(name, selectors):
    parts = name.split("/")
    for selector in selectors:
        pattern = selector.split("/")
        if "**" in pattern:
            literal.refuse()
        if len(parts) == len(pattern) and all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, pattern)):
            return True
    return False


def manifests(workspace, bodies):
    selectors = dependency.workspace(workspace)
    result = {}
    for path, body in bodies.items():
        name = posixpath.dirname(path) or "."
        if name != "." and not selected(name, selectors):
            literal.refuse()
        value = json.loads(body)
        if "name" in value:
            package = records.string(value["name"])
            if not dependency.PACKAGE.fullmatch(package):
                literal.refuse()
        result[name] = value
    return result


def link(owner, reference, name, source):
    relative = reference.removeprefix("link:")
    if not relative or relative.startswith("/") or "\\" in relative:
        literal.refuse()
    if "*" in relative or any(part not in {".", ".."} and not dependency.SEGMENT.fullmatch(part) for part in relative.split("/")):
        literal.refuse()
    target = posixpath.normpath(posixpath.join(owner, relative))
    if target not in source or source[target].get("name") != name:
        literal.refuse()
    return source[target].get("version")


def imported(owner, value, source, snapshots):
    records.mapping(value, FIELDS)
    manifest = source[owner]
    for field in FIELDS:
        declared = manifest.get(field, {})
        locked = records.mapping(value.get(field, {}))
        if set(declared) != set(locked):
            literal.refuse()
        importer_field(owner, (declared, locked), source, snapshots)


def importer_field(owner, declarations, source, snapshots):
    declared, locked = declarations
    for name, requirement in declared.items():
        item = records.mapping(locked[name], {"specifier", "version"})
        if set(item) != {"specifier", "version"} or item["specifier"] != requirement:
            literal.refuse()
        version = records.string(item["version"])
        if requirement.startswith("workspace:"):
            if not version.startswith("link:"):
                literal.refuse()
            resolved = link(owner, version, name, source)
            wanted = requirement.removeprefix("workspace:")
            if wanted not in {"*", "^", "~"}:
                records.identity(f"{name}@{resolved}")
                if not records.satisfies(wanted, resolved):
                    literal.refuse()
        else:
            target = f"{name}@{version}"
            records.snapshot(target)
            if target not in snapshots:
                literal.refuse()
            _, resolved = records.identity(records.snapshot(target))
            if not records.satisfies(requirement, resolved):
                literal.refuse()


def schema(lock, source):
    records.mapping(lock, ROOT)
    if set(lock) != ROOT or lock["lockfileVersion"] != "9.0":
        literal.refuse()
    settings = records.mapping(lock["settings"], set(SETTINGS))
    if settings != SETTINGS or any(type(value) is not bool for value in settings.values()):
        literal.refuse()
    importers = records.mapping(lock["importers"])
    packages = records.mapping(lock["packages"])
    snapshots = records.mapping(lock["snapshots"])
    if set(importers) != set(source):
        literal.refuse()
    for owner, value in importers.items():
        imported(owner, value, source, snapshots)
    downloads = {key: records.package(key, value) for key, value in packages.items()}
    covered = set()
    for key, value in snapshots.items():
        records.snapshots(key, value, lock)
        covered.add(records.snapshot(key))
    if covered != set(packages):
        literal.refuse()
    return downloads


def qualify(workspace_bytes, manifest_bytes, lockfile_bytes):
    configuration = dependency.qualify(workspace_bytes, manifest_bytes)
    lock = literal.parse(lockfile_bytes)
    source = manifests(workspace_bytes, manifest_bytes)
    downloads = schema(lock, source)
    configuration["digests"]["pnpm-lock.yaml"] = hashlib.sha256(lockfile_bytes).hexdigest()
    configuration["files"]["pnpm-lock.yaml"] = lockfile_bytes.decode("ascii")
    return {**configuration, "downloads": downloads}
