from lib.content.lane import codec, shape
from lib.process.confirmation import original, policy
from lib.process.lane import entry, reservation
from lib.refusal import Refusal
from lib.store.lane import documents, registration, state


def keys(intent):
    root = documents.location(intent["target"])
    request = root + "/requests/" + intent["request"]
    return {"registration": root + "/registration.json", "current": root + "/current.json",
            "intent": request + "/intent.json", "custody": request + "/custody.json"}


class Reader(entry.Reader):
    def __init__(self, intent, environ):
        super().__init__(intent, environ)
        self._keys = set(keys(intent).values())


def retained(bucket, intent):
    result, versions = {}, {}
    for name, key in keys(intent).items():
        body, etag = documents.snapshot(bucket, key)
        if body is None:
            raise Refusal("lane confirmation requires all exact retained protected documents")
        value = policy.document(documents.decode(body))
        if body != documents.encode(value):
            raise Refusal("lane confirmation protected document bytes are not canonical")
        result[name] = value
        versions[name] = {"digest": shape.digest(value), "etag": etag}
    return result, versions


def read(pins, bucket, reader):
    intent = pins["dispatch"]
    held, versions = retained(bucket, intent)
    registered = registration.checked(held["registration"])
    if not registered["enabled"] or registration.digest(registered) != intent["registration"] or registered["declaration"]["target"] != intent["target"]:
        raise Refusal("lane confirmation exact enabled registration moved")
    if registered["generation"] != intent["source"]["declaration"]:
        raise Refusal("lane confirmation source configuration generation moved")
    if versions["current"] != pins["current"]:
        raise Refusal("lane confirmation protected current bytes or ETag moved")
    current = state.checked(held["current"], registered)
    if current["phase"] not in ("reserved", "unknown") or current["active"] is None:
        raise Refusal("lane confirmation requires the exact retained active reservation")
    active = state.reservation(held["intent"])
    requested = reservation.requested(intent, active["request"]["intent"].get("artifact"))
    if current["active"] != active or active["request"] != requested or current["revision"] != intent["revision"] or current["observed"] != intent["expected"]:
        raise Refusal("lane confirmation retained intent, request, assessment or settled precondition differs")
    custody = reservation.custody(requested, pins["context"])
    original.basis(intent, {"request": requested, "custody": held["custody"], "actors": pins["actors"]})
    if held["custody"] != custody:
        raise Refusal("lane confirmation original immutable custody moved")
    reference = pins["original"]
    if reader.reference(reference["digest"]) != reference or reader.authority != registered["declaration"]["authorities"]["publication"]:
        raise Refusal("lane confirmation public reader differs from prepared publication authority")
    observed = codec.evidence(reference, reference, reader.read(reference))
    _, record = codec.received(observed)
    entry_value = record["entry"]
    if entry_value["kind"] != "operation" or entry_value["value"]["outcome"]["outcome"] != "unknown":
        raise Refusal("lane confirmation requires the exact original canonical public unknown")
    operation = entry_value["value"]
    if {key: operation[key] for key in ("request", "assessment")} != active:
        raise Refusal("lane confirmation original public request or assessment differs")
    if current["phase"] == "unknown" and current["last"] != operation:
        raise Refusal("lane confirmation retained unknown history differs from original public record")
    return {"registration": registered, "current": current, "intent": active, "custody": custody,
            "original": observed, "versions": versions}
