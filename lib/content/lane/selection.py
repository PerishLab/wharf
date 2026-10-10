from lib.content.lane import identity, material, shape
from lib.refusal import Refusal


def context(value):
    value = shape.object_value(value, "context")
    revision = value["revision"]
    if type(revision) is not int or not 1 <= revision <= 18446744073709551615:
        raise Refusal("lane revision must be a positive u64")
    return {"requested": material.address(value["requested"]), "request": identity.hex_value(value["request"]), "revision": revision, "adaptor": identity.atom(value["adaptor"])}


def observation(value):
    if not isinstance(value, dict):
        raise Refusal("lane observation requires an object")
    outcome = shape.one_of(value.get("outcome"), ("matched", "unmet", "unknown"), "outcome")
    value = shape.object_value(value, outcome)
    if outcome == "matched":
        return {"outcome": outcome, "actual": material.address(value["actual"]), "artifact": material.artifact(value["artifact"])}
    reason = shape.one_of(value["reason"], shape.CONTRACT["reasons"][outcome], "reason")
    return {"outcome": outcome, "reason": reason}


def selection(value):
    value = shape.object_value(value, "selection")
    policy = shape.one_of(value["policy"], ("exact",), "policy")
    requested, observed = context(value["context"]), observation(value["observation"])
    if observed["outcome"] == "matched" and observed["actual"] != requested["requested"]:
        raise Refusal("lane selection must match the entire requested address")
    return {"policy": policy, "context": requested, "observation": observed}


def record(value):
    value = shape.object_value(value, "record")
    if type(value["schema"]) is not int or value["schema"] != 2:
        raise Refusal("unsupported lane evidence schema")
    entry = shape.object_value(value["entry"], "entry")
    shape.one_of(entry["kind"], ("selection",), "record kind for this codec")
    return {"schema": 2, "entry": {"kind": "selection", "value": selection(entry["value"])}}


def reference(value):
    value = shape.object_value(value, "reference")
    return {"authority": identity.atom(value["authority"]), "digest": identity.hex_value(value["digest"])}
