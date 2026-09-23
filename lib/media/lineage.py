import json
import re

from lib.media import depot
from lib.refusal import Refusal

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
