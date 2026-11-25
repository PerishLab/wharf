import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib.content import declaration, executables, resources
from lib.media import release
from lib.process import git
from lib.refusal import Refusal

HOST = resources.read_json("registries.json")["oci"]["host"]
CONTAINERFILE = "Containerfile"
PLATFORM = "linux/amd64"
CHANNEL = "stable"
LABEL = "org.opencontainers.image.version"
TOP_FIELDS = frozenset({"registry", "image", "account", "binary", "file", "web", "components"})
COMPONENT_FIELDS = frozenset({"image", "binary", "file", "web"})
SINGLE_ONLY = ("image", "binary", "file", "web")
COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
IMAGE_SEGMENT = re.compile(r"^[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*$")


@dataclass(frozen=True)
class Component:
    name: str
    kind: str
    image: str
    reference: str
    file: str
    binary: str | None = None
    web: str | None = None
    package: str | None = None
    multi: bool = False


@dataclass(frozen=True)
class Image:
    source: Path
    binary: Path
    name: str
    reference: str
    marker: str
    file: str = CONTAINERFILE


@dataclass(frozen=True)
class SourceImage:
    source: Path
    reference: str
    file: str = CONTAINERFILE


@dataclass(frozen=True)
class WebImage:
    source: Path
    web: str
    package: str
    file: str
    reference: str
    env: dict | None = None


def reference(repository, version):
    owner, name = repository.lower().split("/", 1)
    return f"{HOST}/{owner}/{name}:{version}"


def tagged(image, version):
    return f"{HOST}/{image.lower()}:{version}"


def validate_image(value, owner=None):
    if not isinstance(value, str) or not value:
        raise Refusal("[release.oci] image must be a non-empty owner/name string")
    parts = value.split("/")
    if len(parts) < 2 or any(not IMAGE_SEGMENT.fullmatch(part) for part in parts):
        raise Refusal(f"[release.oci] image {value!r} must be a valid lowercase owner/name")
    if owner is not None and parts[0] != owner:
        raise Refusal(f"[release.oci] image {value!r} must belong to owner {owner!r}")
    return value.lower()


def relative(value, label):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise Refusal(f"[release.oci] {label} {value!r} must be a normalized repo-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or value == "." or any(part in {".", ".."} for part in path.parts):
        raise Refusal(f"[release.oci] {label} {value!r} must be a normalized repo-relative path")
    return value


def untraversed(source, value, label):
    root = Path(source).resolve()
    target = root
    for part in PurePosixPath(value).parts:
        target = target / part
        if target.is_symlink():
            raise Refusal(f"[release.oci] {label} {value!r} must not traverse a symlink")
    if not target.resolve().is_relative_to(root):
        raise Refusal(f"[release.oci] {label} {value!r} must stay inside the product")
    return target


def tracked(source, value, label):
    relative(value, label)
    target = untraversed(source, value, label)
    parts = git(source, "ls-tree", "HEAD", "--", value).split("\t", 1)
    if len(parts) != 2 or parts[1] != value or parts[0].split()[0] not in ("100644", "100755"):
        raise Refusal(f"[release.oci] {label} {value!r} is not a tracked regular file at HEAD")
    if not target.is_file():
        raise Refusal(f"[release.oci] {label} {value!r} is missing")
    return target


def web_manifest(source, web, verify=True):
    relative(web, "web")
    if not untraversed(source, web, "web").is_dir():
        raise Refusal(f"[release.oci] web {web!r} must be a directory inside the product")
    manifest = f"{web}/package.json"
    path = tracked(source, manifest, "web package.json") if verify else untraversed(source, manifest, "web package.json")
    if not path.is_file():
        raise Refusal(f"[release.oci] {manifest!r} is missing")
    try:
        held = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as failure:
        raise Refusal(f"[release.oci] {manifest!r} is not valid JSON") from failure
    if not isinstance(held, dict) or not isinstance(held.get("name"), str) or not held["name"].strip():
        raise Refusal(f"[release.oci] {manifest!r} must declare a non-empty name")
    scripts = held.get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
        raise Refusal(f"[release.oci] {manifest!r} must declare a non-empty scripts.build")
    return held["name"]


def table(source):
    held = declaration.release(source).get("oci")
    if held is None:
        return None
    if not isinstance(held, dict):
        raise Refusal("[release.oci] must be a table")
    extra = sorted(set(held) - TOP_FIELDS)
    if extra:
        raise Refusal(f"[release.oci] holds unknown fields: {', '.join(extra)}")
    if held.get("registry") is not None and held["registry"] != HOST:
        raise Refusal(f"[release.oci].registry must be {HOST!r}, got {held['registry']!r}")
    if held.get("account") is not None and (not isinstance(held["account"], str) or not held["account"].strip()):
        raise Refusal("[release.oci].account must be a non-empty string")
    return held


def non_empty(entry, key, scope):
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refusal(f"{scope}.{key} must be a non-empty string")
    return value


def multi_component(name, entry, owner):
    scope = f"[release.oci.components.{name}]"
    if not isinstance(name, str) or not COMPONENT_NAME.fullmatch(name):
        raise Refusal(f"[release.oci.components] invalid component name {name!r}")
    if not isinstance(entry, dict):
        raise Refusal(f"{scope} must be a table")
    extra = sorted(set(entry) - COMPONENT_FIELDS)
    if extra:
        raise Refusal(f"{scope} holds unknown fields: {', '.join(extra)}")
    if "binary" in entry and "web" in entry:
        raise Refusal(f"{scope} cannot declare both binary and web")
    if "image" not in entry:
        raise Refusal(f"{scope} must declare image")
    image = validate_image(entry["image"], owner)
    if "web" in entry:
        if "file" not in entry:
            raise Refusal(f"{scope} with web must declare file")
        return {"name": name, "kind": "web", "image": image, "file": non_empty(entry, "file", scope), "binary": None, "web": non_empty(entry, "web", scope)}
    file = non_empty(entry, "file", scope) if "file" in entry else CONTAINERFILE
    kind, binary = ("binary", non_empty(entry, "binary", scope)) if "binary" in entry else ("source", None)
    return {"name": name, "kind": kind, "image": image, "file": file, "binary": binary, "web": None}


def multi_entries(held, owner):
    raw = held["components"]
    if not isinstance(raw, dict) or not raw:
        raise Refusal("[release.oci.components] must be a non-empty table")
    mixed = [field for field in SINGLE_ONLY if field in held]
    if mixed:
        raise Refusal(f"[release.oci] cannot mix {', '.join(mixed)} with components")
    parsed, seen = [], set()
    for name in sorted(raw):
        item = multi_component(name, raw[name], owner)
        if item["image"] in seen:
            raise Refusal(f"[release.oci.components] duplicates image {item['image']!r}")
        seen.add(item["image"])
        parsed.append(item)
    return parsed


def single_entry(held, owner, repository):
    if "binary" in held and "web" in held:
        raise Refusal("[release.oci] cannot declare both binary and web")
    image = validate_image(held["image"], owner) if "image" in held else (repository.lower() if repository is not None else None)
    if "web" in held:
        if "file" not in held:
            raise Refusal("[release.oci] with web must declare file")
        return {"image": image, "file": non_empty(held, "file", "[release.oci]"), "binary": None, "web": non_empty(held, "web", "[release.oci]")}
    file = non_empty(held, "file", "[release.oci]") if "file" in held else CONTAINERFILE
    return {"image": image, "file": file, "binary": non_empty(held, "binary", "[release.oci]") if "binary" in held else None, "web": None}


def carried(source):
    held = table(source)
    if held is None:
        return False
    items = multi_entries(held, None) if "components" in held else [dict(single_entry(held, None, None), name="")]
    for item in items:
        tracked(source, item["file"], f"components.{item['name']}.file" if item["name"] else "file")
        if item["web"] is not None:
            web_manifest(source, item["web"], verify=True)
    return True


def resolve_multi(source, held, context, verify):
    owner, version, valid = context
    resolved = []
    for item in multi_entries(held, owner):
        label = f"components.{item['name']}.file"
        tracked(source, item["file"], label) if verify else relative(item["file"], label)
        if item["kind"] == "binary" and item["binary"] not in valid:
            raise Refusal(f"[release.oci.components.{item['name']}] names the executable {item['binary']!r}, which [release].binaries does not list")
        package = web_manifest(source, item["web"], verify=verify) if item["kind"] == "web" else None
        resolved.append(Component(item["name"], item["kind"], item["image"], tagged(item["image"], version), item["file"], item["binary"], item["web"], package, True))
    return resolved


def resolve_single(source, held, context, verify):
    repository, version, listed = context
    owner, product = repository.split("/", 1)
    entry = single_entry(held, owner.lower(), repository)
    tracked(source, entry["file"], "file") if verify else relative(entry["file"], "file")
    ref = tagged(entry["image"], version)
    if entry["web"] is not None:
        return [Component("", "web", entry["image"], ref, entry["file"], None, entry["web"], web_manifest(source, entry["web"], verify=verify), False)]
    if listed:
        binary = entry["binary"] if entry["binary"] is not None else executables.primary(listed, product)
        if binary not in [item.name for item in listed]:
            raise Refusal(f"[release.oci] names the executable {binary!r}, which [release].binaries does not list")
        return [Component("", "binary", entry["image"], ref, entry["file"], binary, None, None, False)]
    if entry["binary"] is not None:
        raise Refusal(f"[release.oci] names the executable {entry['binary']!r}, which [release].binaries does not list")
    return [Component("", "source", entry["image"], ref, entry["file"], None, None, None, False)]


def components(source, repository, version, verify=True):
    held = table(source)
    if held is None:
        return []
    listed = executables.declared(source, release.targets(source))
    if "components" in held:
        return resolve_multi(source, held, (repository.split("/", 1)[0].lower(), version, [item.name for item in listed]), verify)
    return resolve_single(source, held, (repository, version, listed), verify)
