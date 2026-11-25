import json
import re

from lib.media import depot
from lib.refusal import Conflict, Refusal

VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.([1-9]\d*))?")
KINDS = {"alpha": 0, "beta": 1, "rc": 2}
DISCOVERY = frozenset({"scene", "configuration"})
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


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
    try:
        document = json.loads(bucket.get(f"{folder}/manifest.json"))
    except (ValueError, UnicodeError) as error:
        raise Refusal(f"{folder} manifest is not readable JSON") from error
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


def channel_key(channel, kind):
    return f"channels/{channel}/{depot.directory(kind)}/latest.json"


def channel_pointer(bucket, key, kind):
    etag = bucket.head(key)
    try:
        current = json.loads(bucket.get(key)) if etag else None
    except (ValueError, UnicodeError):
        raise Refusal(f"{kind} channel pointer is not readable JSON")
    return current, etag


def discovery(bucket, release, kind, digest):
    if kind not in DISCOVERY:
        raise Refusal(f"{kind!r} does not publish a channel discovery pointer")
    channel = depot.channel_of(release.marker)
    target = (channel, release.marker, kind)
    candidate, _ = pointer(bucket, target)
    candidate = checked(bucket, release, candidate, (channel, kind))
    if candidate["version"] != release.marker or candidate["generation"] != digest:
        raise Refusal(f"{kind} standing changed before channel publication")
    key = channel_key(channel, kind)
    current, etag = channel_pointer(bucket, key, kind)
    if current is not None:
        current = checked(bucket, release, current, (channel, kind))
        if order(current["version"]) > order(candidate["version"]):
            return {"state": "newer", "generation": current["generation"]}
        if current["version"] == candidate["version"] and current["generation"] == candidate["generation"]:
            return {"state": "already-published", "generation": digest}
    try:
        bucket.swap(key, depot.pretty(candidate), etag)
    except Conflict:
        raise Refusal(f"{kind} channel moved during publication; read back and consign again")
    return {"state": "published", "generation": digest}


def checked(bucket, release, held, scope):
    channel, kind = scope
    fields = ("product", "channel", "version", "kind", "generation", "createdAt")
    if not isinstance(held, dict) or any(field not in held for field in fields):
        raise Refusal(f"{kind} pointer is incomplete")
    if any(not isinstance(held[field], str) for field in fields):
        raise Refusal(f"{kind} pointer identity fields must be strings")
    if type(held.get("format")) is not int or held["format"] != depot.FORMAT:
        raise Refusal(f"{kind} pointer format is unsupported")
    stamp = held["createdAt"]
    if not isinstance(stamp, str) or not TIMESTAMP.fullmatch(stamp):
        raise Refusal(f"{kind} pointer timestamp is invalid")
    prior = held.get("previousGeneration")
    if prior is not None and (not isinstance(prior, str) or not depot.DIGEST.fullmatch(prior)):
        raise Refusal(f"{kind} pointer previous generation is invalid")
    product = release.repository.split("/", 1)[1].lower()
    if (held["product"], held["channel"], held["kind"]) != (product, channel, kind):
        raise Refusal(f"{kind} pointer names another projection")
    if not isinstance(held["generation"], str) or not depot.DIGEST.fullmatch(held["generation"]):
        raise Refusal(f"{kind} pointer names an invalid generation")
    if depot.channel_of(held["version"]) != channel:
        raise Refusal(f"{kind} pointer version belongs to another channel")
    record = generation(bucket, (channel, held["version"], kind), held["generation"])
    if record is None:
        raise Refusal(f"{kind} pointer has no immutable manifest")
    expected = depot.pointer(record["document"], depot.source(release), held.get("previousGeneration"), held["createdAt"])
    if held != expected:
        raise Refusal(f"{kind} pointer does not bind its immutable manifest")
    return held


def object_verified(bucket, folder, entry):
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mediaType", "executable"}:
        raise Refusal("configuration manifest carries a malformed object descriptor")
    path = entry["path"]
    digest = entry["sha256"]
    size = entry["size"]
    if not isinstance(path, str) or not isinstance(digest, str) or not depot.DIGEST.fullmatch(digest):
        raise Refusal("configuration manifest carries an invalid object identity")
    if type(size) is not int or size < 0 or type(entry["executable"]) is not bool or not isinstance(entry["mediaType"], str) or not entry["mediaType"]:
        raise Refusal(f"configuration object {path!r} carries invalid metadata")
    depot.check(path)
    key = f"{folder}/objects/{path}"
    if not bucket.exists(key):
        raise Refusal(f"configuration object {path!r} is missing")
    body = bucket.get(key)
    if len(body) != size or depot.sha(body) != digest:
        raise Refusal(f"configuration object {path!r} failed sha256 verification")
    return path


def verified(bucket, release, record):
    document = record["document"]
    if not isinstance(document, dict):
        raise Refusal("configuration manifest is not a JSON object")
    objects = document.get("objects")
    if not isinstance(objects, list) or not objects:
        raise Refusal("configuration manifest holds no objects")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str) for entry in objects):
        raise Refusal("configuration manifest carries a malformed object descriptor")
    expected = depot.identity(release, record["channel"], "configuration")
    if document != depot.manifest(expected, objects):
        raise Refusal("configuration manifest does not match target release identity")
    seen = set()
    for entry in objects:
        path = object_verified(bucket, record["folder"], entry)
        if path in seen:
            raise Refusal(f"configuration manifest repeats object {path!r}")
        seen.add(path)
    return record


def rollback(bucket, release, digest, now):
    if not isinstance(digest, str) or not depot.DIGEST.fullmatch(digest):
        raise Refusal(f"generation digest {digest!r} is not a sha256 hex digest")
    if not isinstance(now, str) or not TIMESTAMP.fullmatch(now):
        raise Refusal("rollback timestamp is invalid")
    channel = depot.channel_of(release.marker)
    record = generation(bucket, (channel, release.marker, "configuration"), digest)
    if record is None:
        raise Refusal(f"configuration generation {digest} does not exist for {release.marker}")
    verified(bucket, release, record)
    key = channel_key(channel, "configuration")
    current, etag = channel_pointer(bucket, key, "configuration")
    if current is not None:
        current = checked(bucket, release, current, (channel, "configuration"))
        if current["version"] == release.marker and current["generation"] == digest:
            return {"state": "already-published", "version": release.marker, "generation": digest, "previous": current["previousGeneration"]}
    prior = current["generation"] if current is not None else None
    written = depot.pointer(record["document"], depot.source(release), prior, now)
    try:
        bucket.swap(key, depot.pretty(written), etag)
    except Conflict:
        raise Refusal("configuration channel moved during rollback; read back and retry")
    return {"state": "rolled-back", "version": release.marker, "generation": digest, "previous": prior}
