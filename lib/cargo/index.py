import gzip
import hashlib
import io
import tarfile
import tomllib

from lib.refusal import Refusal

CRATES_IO = "https://github.com/rust-lang/crates.io-index"
KINDS = {"dependencies": "normal", "dev-dependencies": "dev", "build-dependencies": "build"}


def manifest(blob, name, version):
    path = f"{name}-{version}/Cargo.toml"
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as archive:
        try:
            member = archive.extractfile(path)
        except KeyError:
            member = None
        if member is None:
            raise Refusal(f"{name} {version} carries no {path}")
        return tomllib.loads(member.read().decode())


def requirement(text):
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise Refusal(f"dependency requirement {text!r} is empty")
    return ", ".join(f"^{part}" if part[0].isdigit() else part for part in parts)


def origin(declared, own):
    held = declared.get("registry-index")
    if held is None:
        return CRATES_IO
    if held.removeprefix("sparse+") == own:
        return None
    return held


def dependency(alias, declared, placed, own):
    if isinstance(declared, str):
        declared = {"version": declared}
    if "version" not in declared:
        raise Refusal(f"dependency {alias} names no version")
    package = declared.get("package")
    return {
        "name": alias,
        "req": requirement(declared["version"]),
        "features": list(declared.get("features", [])),
        "optional": bool(declared.get("optional", False)),
        "default_features": bool(declared.get("default-features", True)),
        "target": placed[0],
        "kind": placed[1],
        "registry": origin(declared, own),
        "package": package if package and package != alias else None,
    }


def dependencies(table, own):
    tables = [((None, KINDS[kind]), table.get(kind, {})) for kind in KINDS]
    for target, held in table.get("target", {}).items():
        tables += [((target, KINDS[kind]), held.get(kind, {})) for kind in KINDS]
    found = [dependency(alias, declared, placed, own) for placed, held in tables for alias, declared in held.items()]
    return sorted(found, key=lambda held: (held["name"], held["kind"], held["target"] or ""))


def entry(blob, name, version, own):
    table = manifest(blob, name, version)
    package = table.get("package", {})
    if package.get("name") != name or package.get("version") != version:
        raise Refusal(f"the packaged manifest names {package.get('name')} {package.get('version')}, not {name} {version}")
    found = {
        "name": name,
        "vers": version,
        "deps": dependencies(table, own),
        "cksum": hashlib.sha256(blob).hexdigest(),
        "features": table.get("features", {}),
        "yanked": False,
    }
    if package.get("links"):
        found["links"] = package["links"]
    return found
