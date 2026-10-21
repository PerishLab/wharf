import re

from lib.content.lane import codec, preview
from lib.refusal import Refusal
from lib.store.lane import documents

FIELDS = ("schema", "before", "precondition", "unknown", "operation", "operator", "evidence", "reason")


def checked(value):
    value = documents.object_value(documents.decode(documents.encode(value)), FIELDS)
    if value["schema"] != "wharf.lane.operator-recovery/v1":
        raise Refusal("lane operator recovery schema is unsupported")
    before = value["before"]
    if not isinstance(before, dict) or before.get("schema") != "wharf.lane.current/v1":
        raise Refusal("lane operator recovery requires an original v1 reservation")
    expected = documents.object_value(value["precondition"], ("digest", "etag"))
    codec.read("digest", expected["digest"])
    if not isinstance(expected["etag"], str) or not expected["etag"] or len(expected["etag"]) > 256:
        raise Refusal("lane operator recovery requires the original current ETag")
    operator = documents.object_value(value["operator"], ("id", "login"))
    if type(operator["id"]) is not int or not 1 <= operator["id"] <= 18446744073709551615:
        raise Refusal("lane operator recovery needs a bounded observed operator identity")
    preview.matches(operator["login"], re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"), "operator login")
    codec.read("digest", value["evidence"])
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 512:
        raise Refusal("lane operator recovery needs a bounded explicit reason")
    reference, record = codec.received(value["unknown"])
    entry = record["entry"]
    if entry["kind"] != "operation" or entry["value"]["outcome"]["outcome"] != "unknown":
        raise Refusal("lane operator recovery preserves an exact original public unknown")
    operation = codec.read("operation", value["operation"])
    if operation["outcome"]["outcome"] != "applied" or operation["request"]["intent"]["action"] != "deploy":
        raise Refusal("lane operator recovery supports only explicitly observed deployment material")
    active = {key: operation[key] for key in ("request", "assessment")}
    if before.get("active") != active or any(entry["value"][key] != active[key] for key in active):
        raise Refusal("lane operator recovery differs from the entire original reservation")
    if before.get("phase") not in ("reserved", "unknown") or before.get("last") not in (None, entry["value"]):
        raise Refusal("lane operator recovery cannot replace settled or foreign history")
    return value
