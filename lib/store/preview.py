import copy
import json

from lib.content import canonical
from lib.content.lane import preview
from lib.refusal import Conflict, Refusal

FIELDS = {"schema", "environment", "registration", "revision", "phase", "active", "last", "actual", "last_verified"}
PHASES = ("absent", "active", "verified", "degraded", "failed", "discarded", "unknown")


def location(intent):
    preview.request(intent)
    return f"preview/v1/{intent['repository']}/{intent['app']}/{intent['name']}"


def initial(intent):
    return {
        "schema": "wharf.preview.current/v1", "environment": preview.environment(intent),
        "registration": intent["registration"], "revision": 0, "phase": "absent",
        "active": None, "last": None, "actual": None, "last_verified": None,
    }


def checked(value, intent):
    preview.shape(value, FIELDS, "current record")
    preview.schema(value["schema"], "current")
    if value["environment"] != preview.environment(intent) or value["registration"] != intent["registration"]:
        raise Refusal("preview current record belongs to a different environment or registration")
    if type(value["revision"]) is not int or value["revision"] < 0 or value["phase"] not in PHASES:
        raise Refusal("preview current revision or phase is malformed")
    active, last = value["active"], value["last"]
    if (active is not None) != (value["phase"] in ("active", "unknown")):
        raise Refusal("preview active request and phase disagree")
    if last is None and (value["revision"] != 0 or value["phase"] not in ("absent", "active") or value["actual"] is not None or value["last_verified"] is not None):
        raise Refusal("preview record without history must be an initial state")
    if value["phase"] == "absent" and last is not None:
        raise Refusal("preview absent state cannot discard its history")
    if active is not None:
        preview.request(active)
        if preview.environment(active) != value["environment"] or active["registration"] != value["registration"] or active["revision"] != value["revision"] or active["operation"] == "inspect":
            raise Refusal("preview active request identity disagrees with current record")
    if last is not None:
        preview.shape(last, {"request", "result"}, "last operation")
        preview.result(last["result"], last["request"])
        if preview.environment(last["request"]) != value["environment"] or last["request"]["registration"] != value["registration"]:
            raise Refusal("preview last operation belongs to a different target")
        unknown = last["result"]["outcome"] == "unknown"
        if unknown and (active != last["request"] or value["phase"] != "unknown"):
            raise Refusal("preview unknown operation must remain active")
        if not unknown and last["request"]["revision"] + 1 != value["revision"]:
            raise Refusal("preview settled operation revision disagrees")
        if value["phase"] != "active" and value["phase"] != last["result"]["outcome"]:
            raise Refusal("preview phase disagrees with its last result")
        observation = last["result"]["provider"]
        if observation["state"] in ("present", "absent") and value["actual"] != observation["deployment"]:
            raise Refusal("preview actual deployment disagrees with provider observation")
        if last["result"]["outcome"] == "verified" and value["last_verified"] != observation["deployment"]:
            raise Refusal("preview verified deployment disagrees with byte evidence")
    for key in ("actual", "last_verified"):
        if value[key] is not None:
            preview.deployment(value[key])
    return value


def started(current, intent):
    preview.request(intent)
    checked(current, intent)
    if intent["operation"] == "inspect":
        raise Refusal("preview inspect is read-only and cannot reserve a mutation")
    active = current["active"]
    if active is not None:
        if active == intent:
            return copy.deepcopy(current)
        raise Refusal("preview environment is busy; unknown operations never expire into write permission")
    if current["last"] is not None and current["last"]["request"] == intent:
        return copy.deepcopy(current)
    if current["revision"] != intent["revision"]:
        raise Refusal("preview request revision is stale")
    return dict(copy.deepcopy(current), phase="active", active=copy.deepcopy(intent))


def finished(current, intent, result):
    checked(current, intent)
    preview.result(result, intent)
    previous = {"request": intent, "result": result}
    if current["active"] is None and current["last"] == previous:
        return copy.deepcopy(current)
    if current["active"] != intent:
        raise Refusal("preview result has no matching active request")
    unknown = result["outcome"] == "unknown"
    updated = dict(copy.deepcopy(current), phase=result["outcome"], last=copy.deepcopy(previous))
    if result["provider"]["state"] in ("present", "absent"):
        updated["actual"] = copy.deepcopy(result["provider"]["deployment"])
    if result["outcome"] == "verified":
        updated["last_verified"] = copy.deepcopy(updated["actual"])
    if not unknown:
        updated.update(active=None, revision=current["revision"] + 1)
    return checked(updated, intent)


def read(bucket, intent):
    key = f"{location(intent)}/current.json"
    body, etag = bucket.snapshot(key)
    if body is None:
        return initial(intent), None
    if len(body) > 65536:
        raise Refusal("preview current record exceeds its 64 KiB budget")
    try:
        value = json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise Refusal("preview current record is not valid JSON") from error
    return checked(value, intent), etag


def unique(pairs):
    held = {}
    for key, value in pairs:
        if key in held:
            raise Refusal("preview current record repeats a JSON field")
        held[key] = value
    return held


def immutable(bucket, key, value):
    body = canonical.encode(value)
    try:
        bucket.create(key, body)
    except Conflict:
        if bucket.get(key) != body:
            raise Refusal("preview immutable request identity already names different bytes")


def reserve(bucket, intent, target):
    preview.admitted(intent, target)
    current, etag = read(bucket, intent)
    proposed = started(current, intent)
    prefix = location(intent)
    immutable(bucket, f"{prefix}/requests/{intent['request']}/intent.json", intent)
    if proposed != current:
        bucket.swap(f"{prefix}/current.json", canonical.encode(proposed), etag)
    return proposed


def record(bucket, intent, result):
    preview.request(intent)
    current, etag = read(bucket, intent)
    proposed = finished(current, intent, result)
    prefix = location(intent)
    digest = canonical.digest(result)
    immutable(bucket, f"{prefix}/requests/{intent['request']}/{digest}.json", result)
    if proposed != current:
        bucket.swap(f"{prefix}/current.json", canonical.encode(proposed), etag)
    return proposed
