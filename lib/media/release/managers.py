import hashlib
import json

from lib.refusal import Refusal


def matches(body, entry):
    return body is not None and len(body) == entry["size"] and hashlib.sha256(body).hexdigest() == entry["sha256"]


def reference(held, rendered):
    if not isinstance(held, dict) or set(held) != set(rendered) or any(held[name] != rendered[name] for name in ("name", "mime", "url")):
        raise Refusal("canonical manager reference differs from its platform or authority")
    if type(held["size"]) is not int or held["size"] < 0 or not isinstance(held["sha256"], str) or len(held["sha256"]) != 64 or set(held["sha256"]) - set("0123456789abcdef"):
        raise Refusal("canonical manager content reference is malformed")
    return held


def pending(bucket, rooted, held):
    expected = held.get("managers")
    if not isinstance(expected, dict) or set(expected) != set(rooted):
        raise Refusal("canonical manager platforms differ from the current pointer")
    writes = []
    for platform, (key, body, rendered) in rooted.items():
        entry = reference(expected[platform], rendered)
        current, _ = bucket.snapshot(key)
        if matches(current, entry):
            continue
        if rendered != entry or not matches(body, entry):
            raise Refusal(f"{key} needs explicit recovery with the bytes named by the current pointer")
        writes.append((key, body, entry))
    return writes


def lead(bucket, rooted, moved, marker):
    if not rooted:
        return moved
    try:
        held = json.loads(bucket.get(moved["pointer"]))
        if held["releaseVersion"] != marker:
            return moved
        writes = pending(bucket, rooted, held)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise Refusal("canonical manager pointer is malformed") from error
    for key, body, entry in writes:
        bucket.put(key, body, {"Content-Type": entry["mime"]})
    return {**moved, "managers": sorted(key for key, _, _ in rooted.values())}
