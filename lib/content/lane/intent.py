from lib.content.lane import assessment, material, selection, shape
from lib.refusal import Refusal


def intent(value):
    value, action = shape.tagged(value, "action", shape.CONTRACT["actions"], "intent")
    if action == "build":
        value["source"] = material.source(value["source"])
        value["build"] = material.build(value["build"])
    elif action in ("publish", "install", "deploy"):
        value["artifact"] = material.artifact(value["artifact"])
    return value


def request(value):
    value = shape.object_value(value, "request", ("expected",))
    value["context"] = selection.context(value["context"])
    value["intent"] = intent(value["intent"])
    expected = None if value["expected"] is None else assessment.checked_state(value["expected"])
    action = value["intent"]["action"]
    allowed = shape.CONTRACT["preconditions"][action]
    if (None if expected is None else expected["state"]) not in allowed:
        raise Refusal("lane action needs its explicit atomic precondition")
    value["expected"] = expected
    return value


def admitted(requested, assessed):
    requested, assessed = request(requested), assessment.assessment(assessed)
    if shape.digest(requested) != assessed["request"]:
        return "conflict"
    if requested["intent"]["action"] not in assessed["capabilities"]:
        return "unsupported"
    held = assessed["conditions"]
    if held["authorization"]["state"] != "granted":
        return "denied" if held["authorization"]["state"] == "denied" else "unverified"
    if requested["intent"]["action"] in ("inspect", "build"):
        return None
    if held["writer"] != "idle":
        return "conflict" if held["writer"] == "active" else "unavailable"
    if held["observed"]["state"] == "unknown":
        return "unverified"
    if requested["expected"] != held["observed"]:
        return "conflict"
    return None


def declared(declaration, requested, assessed):
    declaration = material.declaration(declaration)
    requested, assessed = request(requested), assessment.assessment(assessed)
    context = requested["context"]
    if declaration["target"] != context["requested"] or declaration["adaptor"] != context["adaptor"] or declaration["capabilities"] != assessed["capabilities"]:
        return "conflict"
    authorization = assessed["conditions"]["authorization"]
    if authorization["state"] != "unknown" and authorization["evidence"]["authority"] != declaration["authorities"]["authorization"]:
        return "unverified"
    return admitted(requested, assessed)
