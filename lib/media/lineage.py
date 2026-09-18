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


def versions(bucket, directory):
    found = []
    for channel in bucket.prefixes("channels/"):
        name = channel.split("/")[1]
        for held in bucket.prefixes(f"{channel}{directory}/versions/"):
            version = held.rstrip("/").rsplit("/", 1)[1]
            if VERSION.fullmatch(version):
                found.append((name, version))
    return found


def nearest(bucket, directory, version):
    candidates = [held for held in versions(bucket, directory) if line(held[1]) == line(version) and order(held[1]) < order(version)]
    if not candidates:
        raise Refusal(f"no {directory} generation below {version} on its line; pass --full or --from")
    return max(candidates, key=lambda held: order(held[1]))


def pointer(bucket, channel, version):
    key = f"{depot.route(channel, version)}/latest.json"
    etag = bucket.head(key)
    return (json.loads(bucket.get(key)), etag) if etag else (None, None)


def generation(bucket, channel, version, digest):
    folder = f"{depot.route(channel, version)}/generations/{digest}"
    if not bucket.exists(f"{folder}/manifest.json"):
        return None
    document = json.loads(bucket.get(f"{folder}/manifest.json"))
    if depot.sha(depot.compact(document)) != digest:
        raise Refusal(f"{folder} does not hold the generation it names")
    return {"channel": channel, "version": version, "generation": digest, "folder": folder, "document": document}


def standing(bucket, channel, version):
    held, _ = pointer(bucket, channel, version)
    return generation(bucket, channel, version, held["generation"]) if held else None


def named(bucket, directory, spec):
    if depot.DIGEST.fullmatch(spec):
        for channel, version in versions(bucket, directory):
            found = generation(bucket, channel, version, spec)
            if found:
                return found
        raise Refusal(f"no {directory} generation {spec}")
    channel, _, version = spec.partition("/")
    found = standing(bucket, channel, version) if version else None
    if not found:
        raise Refusal(f"--from {spec!r} names no standing generation; use <channel>/<version> or a generation digest")
    return found


def base(bucket, target, spec=None):
    channel, version = target
    if spec:
        return named(bucket, depot.DIRECTORY, spec)
    own = standing(bucket, channel, version)
    if own:
        return own
    return standing(bucket, *nearest(bucket, depot.DIRECTORY, version))
