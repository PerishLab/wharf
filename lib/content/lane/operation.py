from lib.content.lane import assessment, intent, material as content, selection, shape
from lib.refusal import Refusal


def material(value):
    value, tag = shape.tagged(value, "state", ("present", "absent"), "material")
    if tag == "present":
        value["artifact"] = content.artifact(value["artifact"])
    return value


def outcome(value):
    value, tag = shape.tagged(value, "outcome", ("applied", "refused", "unknown"), "operation")
    if tag == "applied":
        value["material"] = material(value["material"])
        value["writer"] = assessment.writer(value["writer"])
    else:
        reasons = shape.CONTRACT["reasons"]
        value["reason"] = shape.one_of(value["reason"], reasons["unmet"] + reasons["unknown"], "reason")
    return value


def settles(requested, assessed, actual):
    action, desired = requested["intent"]["action"], requested["intent"]
    if action == "inspect":
        observed = assessed["conditions"]["observed"]
        if actual["state"] == "absent":
            return observed["state"] == "absent"
        return observed["state"] == "present" and observed["digest"] == actual["artifact"]["digest"]
    if action == "build":
        return actual["state"] == "present" and actual["artifact"]["source"] == desired["source"] and actual["artifact"]["build"] == desired["build"]
    if action in ("publish", "install", "deploy"):
        return actual["state"] == "present" and actual["artifact"] == desired["artifact"]
    return actual["state"] == "absent"


def operation(value):
    value = shape.object_value(value, "operation")
    requested, assessed, actual = intent.request(value["request"]), assessment.assessment(value["assessment"]), outcome(value["outcome"])
    refusal = intent.admitted(requested, assessed)
    if actual["outcome"] == "applied":
        if refusal is not None:
            raise Refusal(f"lane operation was not admitted: {refusal}")
        if requested["intent"]["action"] not in ("inspect", "build") and actual["writer"] != "idle":
            raise Refusal("uncertain or active writer cannot settle a lane mutation")
        if not settles(requested, assessed, actual["material"]):
            raise Refusal("lane material does not settle the exact intent")
    else:
        reasons = shape.CONTRACT["reasons"]["unknown" if actual["outcome"] == "unknown" else "unmet"]
        shape.one_of(actual["reason"], reasons, "operation reason")
        if refusal is not None and refusal != actual["reason"]:
            raise Refusal("lane outcome must preserve its admission refusal")
    return {"request": requested, "assessment": assessed, "outcome": actual}



def confirmation(value):
    value = shape.object_value(value, "confirmation")
    original = selection.reference(value["original"])
    authorization = selection.reference(value["authorization"])
    observed = operation(value["operation"])
    if original == authorization or original["authority"] != authorization["authority"]:
        raise Refusal("lane confirmation needs distinct original and new public authorization references under one publication authority")
    admission = observed["assessment"]["conditions"]["authorization"]
    if admission["state"] == "granted" and admission["evidence"] == authorization:
        raise Refusal("lane confirmation cannot reuse its original admission reference as new authorization")
    if observed["request"]["intent"]["action"] in ("inspect", "build") or observed["outcome"]["outcome"] != "applied":
        raise Refusal("lane confirmation needs an exact applied mutation observation")
    return {"original": original, "authorization": authorization, "operation": observed}


def record(value):
    value = shape.object_value(value, "record")
    entry = shape.object_value(value["entry"], "entry")
    kind = shape.one_of(entry["kind"], ("selection", "operation", "confirmation"), "record kind")
    schema = 3 if kind == "confirmation" else 2
    if type(value["schema"]) is not int or value["schema"] != schema:
        raise Refusal("unsupported lane evidence schema")
    reader = {"selection": selection.selection, "operation": operation, "confirmation": confirmation}[kind]
    return {"schema": schema, "entry": {"kind": kind, "value": reader(entry["value"])}}
