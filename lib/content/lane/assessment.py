from lib.content.lane import identity, material, selection, shape
from lib.refusal import Refusal


def state(value):
    value, tag = shape.tagged(value, "state", ("absent", "present", "unknown"), "state")
    if tag == "present":
        value["revision"] = shape.revision(value["revision"], 0)
        value["digest"] = identity.hex_value(value["digest"])
    return value


def checked_state(value):
    value = state(value)
    if value["state"] == "present" and value["revision"] == 0:
        raise Refusal("lane observed revision must be positive")
    return value


def authorization(value):
    value, tag = shape.tagged(value, "state", ("granted", "denied", "unknown"), "authorization")
    if tag != "unknown":
        value["evidence"] = selection.reference(value["evidence"])
    return value


def writer(value):
    return shape.one_of(value, ("idle", "active", "unknown"), "writer")


def conditions(value):
    value = shape.object_value(value, "conditions")
    return {"authorization": authorization(value["authorization"]), "writer": writer(value["writer"]), "observed": checked_state(value["observed"])}


def assessment(value):
    value = shape.object_value(value, "assessment")
    return {"request": identity.hex_value(value["request"]), "capabilities": material.capabilities(value["capabilities"]), "conditions": conditions(value["conditions"])}
