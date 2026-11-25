import datetime
import os
import re

from lib.content import marker
from lib.content.static import evidence
from lib.media import depot
from lib.refusal import Refusal

BASE = depot.LAYOUT["source"].format(name="plumb")
CHANNEL = "stable"
PRODUCT = "plumb"
KIND = "configuration"
VARIABLE = "WHARF_CONFIGURATION"
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
POINTER = frozenset({"format", "product", "channel", "version", "marker", "kind", "generation", "manifest", "previousGeneration", "createdAt"})
MANIFEST = frozenset({"format", "product", "channel", "version", "marker", "kind", "objects"})
REFERENCE = frozenset({"url", "sha256", "size"})
ENTRY = frozenset({"path", "sha256", "size", "mediaType", "executable"})
PINNED = frozenset({"channel", "version", "generation", "manifest"})
SHARED = ("format", "product", "channel", "version", "kind", "marker")


def digest(value, label):
    if not isinstance(value, str) or not depot.DIGEST.fullmatch(value):
        raise Refusal(f"pinned configuration {label} is not 64 lowercase hex")
    return value


def version_checked(value):
    if not isinstance(value, str) or not marker.holds(value) or marker.channel(value) != CHANNEL:
        raise Refusal("pinned configuration version must be a stable release marker")
    return value


def timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise Refusal("pinned configuration timestamp is invalid")
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise Refusal("pinned configuration timestamp is invalid") from error
    return value


def marker_checked(held, version):
    if not isinstance(held, dict) or frozenset(held) != frozenset({"name", "sha256"}):
        raise Refusal("pinned configuration marker is malformed")
    if held["name"] != version:
        raise Refusal("pinned configuration marker disagrees with version")
    digest(held["sha256"], "marker sha256")
    return {"name": version, "sha256": held["sha256"]}


def manifest_ref(held, version, generation):
    if not isinstance(held, dict) or frozenset(held) != REFERENCE:
        raise Refusal("pinned configuration manifest reference is malformed")
    expected = f"{BASE}/{depot.route(CHANNEL, version, KIND)}/generations/{generation}/manifest.json"
    if held["url"] != expected:
        raise Refusal("pinned configuration manifest url is not canonical")
    size = held["size"]
    if type(size) is not int or not (0 < size <= evidence.LIMIT):
        raise Refusal("pinned configuration manifest size is invalid")
    digest(held["sha256"], "manifest sha256")
    return {"url": expected, "sha256": held["sha256"], "size": size}


def pointer_checked(held):
    if not isinstance(held, dict) or frozenset(held) != POINTER:
        raise Refusal("pinned configuration pointer is malformed")
    if type(held["format"]) is not int or held["format"] != depot.FORMAT:
        raise Refusal("pinned configuration pointer format is unsupported")
    if held["product"] != PRODUCT or held["channel"] != CHANNEL or held["kind"] != KIND:
        raise Refusal("pinned configuration pointer names another projection")
    version = version_checked(held["version"])
    generation = digest(held["generation"], "generation")
    timestamp(held["createdAt"])
    prior = held["previousGeneration"]
    if prior is not None:
        digest(prior, "previousGeneration")
    marker_checked(held["marker"], version)
    manifest_ref(held["manifest"], version, generation)
    return held


def objects_checked(objects):
    if not isinstance(objects, list) or not objects:
        raise Refusal("pinned configuration manifest holds no objects")
    paths = []
    for entry in objects:
        if not isinstance(entry, dict) or frozenset(entry) != ENTRY:
            raise Refusal("pinned configuration object descriptor is malformed")
        path = entry["path"]
        if not isinstance(path, str):
            raise Refusal("pinned configuration object path must be a string")
        depot.check(path)
        digest(entry["sha256"], "object sha256")
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise Refusal("pinned configuration object size is invalid")
        if not isinstance(entry["mediaType"], str) or not entry["mediaType"] or type(entry["executable"]) is not bool:
            raise Refusal("pinned configuration object metadata is invalid")
        paths.append(path)
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise Refusal("pinned configuration objects are not strictly sorted and unique")


def read(reader, url):
    try:
        body = reader(url)
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refusal("pinned configuration authority is unreadable") from error
    if not isinstance(body, bytes) or len(body) > evidence.LIMIT:
        raise Refusal("pinned configuration response is invalid or exceeds bounds")
    return body


def manifest_checked(body, ref, version, generation):
    if len(body) != ref["size"] or depot.sha(body) != ref["sha256"]:
        raise Refusal("pinned configuration manifest bytes differ from reference")
    document = evidence.decode(body)
    if not isinstance(document, dict) or frozenset(document) != MANIFEST:
        raise Refusal("pinned configuration manifest fields differ")
    if type(document["format"]) is not int or document["format"] != depot.FORMAT:
        raise Refusal("pinned configuration manifest format is unsupported")
    if document["product"] != PRODUCT or document["channel"] != CHANNEL or document["version"] != version or document["kind"] != KIND:
        raise Refusal("pinned configuration manifest identity disagrees")
    marker_checked(document["marker"], version)
    objects_checked(document["objects"])
    if depot.sha(depot.compact(document)) != generation:
        raise Refusal("pinned configuration manifest generation digest disagrees")
    return document


def checked(pinned):
    if not isinstance(pinned, dict) or frozenset(pinned) != PINNED:
        raise Refusal("pinned configuration descriptor is malformed")
    if pinned["channel"] != CHANNEL:
        raise Refusal("pinned configuration channel must be stable")
    version = version_checked(pinned["version"])
    generation = digest(pinned["generation"], "generation")
    ref = manifest_ref(pinned["manifest"], version, generation)
    return {"channel": CHANNEL, "version": version, "generation": generation, "manifest": ref}


def discover(reader):
    channel = pointer_checked(evidence.decode(read(reader, f"{BASE}/channels/{CHANNEL}/configurations/latest.json")))
    version = channel["version"]
    standing = pointer_checked(evidence.decode(read(reader, f"{BASE}/{depot.route(CHANNEL, version, KIND)}/latest.json")))
    if any(standing[field] != channel[field] for field in SHARED):
        raise Refusal("pinned configuration version standing disagrees with channel pointer")
    if standing["generation"] == channel["generation"] and standing["manifest"] != channel["manifest"]:
        raise Refusal("pinned configuration version standing manifest disagrees with channel pointer")
    body = read(reader, channel["manifest"]["url"])
    document = manifest_checked(body, channel["manifest"], version, channel["generation"])
    if depot.pointer(document, BASE, channel["previousGeneration"], channel["createdAt"]) != channel:
        raise Refusal("pinned configuration pointer does not bind its immutable manifest")
    return {"channel": CHANNEL, "version": version, "generation": channel["generation"], "manifest": dict(channel["manifest"])}


def verify(pinned, reader):
    held = checked(pinned)
    body = read(reader, held["manifest"]["url"])
    manifest_checked(body, held["manifest"], held["version"], held["generation"])
    return held


def parse(raw):
    if raw is None or raw == "" or raw == "null":
        return None
    if not isinstance(raw, str):
        raise Refusal("pinned configuration must be a JSON string")
    return checked(evidence.decode(raw))


def from_env(env=None):
    source = os.environ if env is None else env
    if VARIABLE not in source:
        return None
    return parse(source[VARIABLE])
