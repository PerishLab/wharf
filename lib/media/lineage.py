import json
import re

from lib.media import depot
from lib.refusal import Conflict, Refusal

VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.([1-9]\d*))?")
KINDS = {"alpha": 0, "beta": 1, "rc": 2}


def order(version):
    matched = VERSION.fullmatch(version)
    if not matched:
        raise Refusal(f"{version!r} is not a release version")
    major, minor, patch, kind, number = matched.groups()
    stage = (1, 0, 0) if kind is None else (0, KINDS[kind], int(number))
    return (int(major), int(minor), int(patch)) + stage


def line(version):
    return order(version)[:3]


def versions(bucket, kind):
    directory = depot.directory(kind)
    found = []
    for channel in bucket.prefixes("channels/"):
        name = channel.split("/")[1]
        for held in bucket.prefixes(f"{channel}{directory}/versions/"):
            version = held.rstrip("/").rsplit("/", 1)[1]
            if VERSION.fullmatch(version):
                found.append((name, version))
    return found


def nearest(bucket, kind, version):
    candidates = [held for held in versions(bucket, kind) if line(held[1]) == line(version) and order(held[1]) < order(version)]
    if not candidates:
        raise Refusal(f"no {kind} generation below {version} on its line; pass --full or --from")
    return max(candidates, key=lambda held: order(held[1]))


def pointer(bucket, target):
    key = f"{depot.route(*target)}/latest.json"
    etag = bucket.head(key)
    return (json.loads(bucket.get(key)), etag) if etag else (None, None)


def generation(bucket, target, digest):
    channel, version, _ = target
    folder = f"{depot.route(*target)}/generations/{digest}"
    if not bucket.exists(f"{folder}/manifest.json"):
        return None
    document = json.loads(bucket.get(f"{folder}/manifest.json"))
    if depot.sha(depot.compact(document)) != digest:
        raise Refusal(f"{folder} does not hold the generation it names")
    return {"channel": channel, "version": version, "generation": digest, "folder": folder, "document": document}


def standing(bucket, target):
    held, _ = pointer(bucket, target)
    return generation(bucket, target, held["generation"]) if held else None


def below(bucket, target):
    _, version, kind = target
    return standing(bucket, (*nearest(bucket, kind, version), kind))


def parent(bucket, target):
    own = standing(bucket, target)
    if own:
        return own
    _, version, kind = target
    below = [held for held in versions(bucket, kind) if order(held[1]) < order(version) and (line(held[1]) == line(version) or held[0] == "stable")]
    if not below:
        return None
    return standing(bucket, (*max(below, key=lambda held: order(held[1])), kind))


def discovery(bucket, release, digest):
    channel = depot.channel_of(release.marker)
    target = (channel, release.marker, "scene")
    candidate, _ = pointer(bucket, target)
    candidate = checked(bucket, release, candidate, channel)
    if candidate["version"] != release.marker or candidate["generation"] != digest:
        raise Refusal("scene standing changed before channel publication")
    key = f"channels/{channel}/scenes/latest.json"
    etag = bucket.head(key)
    try:
        current = json.loads(bucket.get(key)) if etag else None
    except (ValueError, UnicodeError):
        raise Refusal("scene channel pointer is not readable JSON")
    if current is not None:
        current = checked(bucket, release, current, channel)
        if order(current["version"]) > order(candidate["version"]):
            return {"state": "newer", "generation": current["generation"]}
        if current == candidate:
            return {"state": "already-published", "generation": digest}
    try:
        bucket.swap(key, depot.pretty(candidate), etag)
    except Conflict:
        raise Refusal("scene channel moved during publication; read back and consign again")
    return {"state": "published", "generation": digest}


def checked(bucket, release, held, channel):
    fields = ("product", "channel", "version", "kind", "generation", "createdAt")
    if not isinstance(held, dict) or any(field not in held for field in fields):
        raise Refusal("scene pointer is incomplete")
    if any(not isinstance(held[field], str) for field in fields):
        raise Refusal("scene pointer identity fields must be strings")
    if type(held.get("format")) is not int or held["format"] != depot.FORMAT:
        raise Refusal("scene pointer format is unsupported")
    stamp = held["createdAt"]
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stamp):
        raise Refusal("scene pointer timestamp is invalid")
    prior = held.get("previousGeneration")
    if prior is not None and (not isinstance(prior, str) or not depot.DIGEST.fullmatch(prior)):
        raise Refusal("scene pointer previous generation is invalid")
    product = release.repository.split("/", 1)[1].lower()
    if (held["product"], held["channel"], held["kind"]) != (product, channel, "scene"):
        raise Refusal("scene pointer names another projection")
    if not isinstance(held["generation"], str) or not depot.DIGEST.fullmatch(held["generation"]):
        raise Refusal("scene pointer names an invalid generation")
    if depot.channel_of(held["version"]) != channel:
        raise Refusal("scene pointer version belongs to another channel")
    record = generation(bucket, (channel, held["version"], "scene"), held["generation"])
    if record is None:
        raise Refusal("scene pointer has no immutable manifest")
    expected = depot.pointer(record["document"], depot.source(release), held.get("previousGeneration"), held["createdAt"])
    if held != expected:
        raise Refusal("scene pointer does not bind its immutable manifest")
    return held
