import base64
import binascii
import re

from lib.check import dependency
from lib.check.locking.literal import refuse

EXACT = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?")
PACKAGE_FIELDS = {"resolution", "engines", "hasBin", "cpu", "os", "libc", "peerDependencies", "peerDependenciesMeta"}
SNAPSHOT_FIELDS = {"dependencies", "optionalDependencies", "optional", "transitivePeerDependencies"}


def mapping(value, allowed=None):
    if not isinstance(value, dict) or len(value) > 4096:
        refuse()
    if allowed is not None and set(value) - allowed:
        refuse()
    return value


def string(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or "${" in value:
        refuse()
    return value


def boolean(value):
    if type(value) is not bool:
        refuse()


def names(value):
    for name, item in mapping(value).items():
        if not dependency.PACKAGE.fullmatch(name) or len(name) > 214:
            refuse()
        string(item)
    return value


def strings(value):
    if not isinstance(value, list) or len(value) > 128:
        refuse()
    for item in value:
        string(item)


def identity(value):
    value = string(value)
    name, separator, version = value.rpartition("@")
    if not separator or not dependency.PACKAGE.fullmatch(name) or not EXACT.fullmatch(version):
        refuse()
    if len(name) > 214:
        refuse()
    if "-" in version:
        for part in version.split("-", 1)[1].split("."):
            if part.isdigit() and len(part) > 1 and part.startswith("0"):
                refuse()
    return name, version


def snapshot(value, depth=0, packages=None):
    value = string(value)
    if depth > 8:
        refuse()
    base = value.split("(", 1)[0]
    identity(base)
    if packages is not None and base not in packages:
        refuse()
    suffix = value[len(base):]
    while suffix:
        if not suffix.startswith("("):
            refuse()
        level, end = 0, None
        for index, char in enumerate(suffix):
            level += (char == "(") - (char == ")")
            if level == 0:
                end = index
                break
        if end is None:
            refuse()
        snapshot(suffix[1:end], depth + 1, packages)
        suffix = suffix[end + 1:]
    return base


def satisfies(requirement, version):
    prefix = requirement[0] if requirement[0] in "^~" else ""
    wanted = requirement[1:] if prefix else requirement
    if "-" in wanted or "-" in version:
        return not prefix and wanted == version
    parts = tuple(int(part) for part in wanted.split("."))
    lower = parts + (0,) * (3 - len(parts))
    actual = tuple(int(part) for part in version.split("."))
    if not prefix and len(parts) == 3:
        return actual == lower
    index = 0 if len(parts) == 1 else 1
    if prefix == "^":
        index = next((i for i, value in enumerate(parts) if value), len(parts) - 1)
    upper = lower[:index] + (lower[index] + 1,) + (0,) * (2 - index)
    return lower <= actual < upper


def integrity(value):
    value = string(value)
    if not value.startswith("sha512-"):
        refuse()
    try:
        decoded = base64.b64decode(value[7:], validate=True)
    except (ValueError, binascii.Error):
        refuse()
    if len(decoded) != 64 or base64.b64encode(decoded).decode() != value[7:]:
        refuse()


def location(key, resolution):
    name, version = identity(key)
    mapping(resolution, {"integrity", "tarball"})
    integrity(resolution.get("integrity"))
    if name.startswith("@perishlab/"):
        origin = dependency.REGISTRIES["@perishlab"]
        prefix = f"{origin}download/{name}/{version}/"
        url = string(resolution.get("tarball"))
        if not url.startswith(prefix) or not re.fullmatch(r"[0-9a-f]{40}", url[len(prefix):]):
            refuse()
    else:
        url = f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz"
        if resolution.get("tarball", url) != url:
            refuse()
    return url


def package(key, value):
    mapping(value, PACKAGE_FIELDS)
    url = location(key, value.get("resolution"))
    if "hasBin" in value:
        boolean(value["hasBin"])
    for field in {"cpu", "os", "libc"}.intersection(value):
        strings(value[field])
    if "engines" in value:
        names(value["engines"])
    if "peerDependencies" in value:
        names(value["peerDependencies"])
    for name, metadata in mapping(value.get("peerDependenciesMeta", {})).items():
        if not dependency.PACKAGE.fullmatch(name):
            refuse()
        mapping(metadata, {"optional"})
        if "optional" in metadata:
            boolean(metadata["optional"])
    return url


def snapshots(key, value, lock):
    base = snapshot(key, packages=lock["packages"])
    if base not in lock["packages"]:
        refuse()
    mapping(value, SNAPSHOT_FIELDS)
    if "optional" in value:
        boolean(value["optional"])
    if "transitivePeerDependencies" in value:
        strings(value["transitivePeerDependencies"])
    for field in {"dependencies", "optionalDependencies"}.intersection(value):
        for name, version in names(value[field]).items():
            target = f"{name}@{version}"
            snapshot(target)
            if target not in lock["snapshots"]:
                refuse()
