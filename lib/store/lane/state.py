import hashlib

from lib.content import canonical
from lib.content.lane import codec, shape
from lib.refusal import Refusal
from lib.store.lane import documents, recovery, registration

FIELDS = ("schema", "target", "registration", "revision", "phase", "active", "last", "observed")


def initial(registered):
    registered = registration.checked(registered)
    return {"schema": "wharf.lane.current/v1", "target": registered["declaration"]["target"],
            "registration": registration.digest(registered), "revision": 1, "phase": "vacant",
            "active": None, "last": None, "observed": {"state": "absent"}}


def reservation(value):
    value = documents.object_value(value, ("request", "assessment"))
    return {"request": codec.read("request", value["request"]), "assessment": codec.read("assessment", value["assessment"])}


def matches(value, current, registered):
    requested = value["request"]
    if requested["context"]["requested"] != current["target"]:
        raise Refusal("lane history belongs to another exact target")
    registration.admitted(registered, requested, value["assessment"])
    if requested["intent"]["action"] not in ("deploy", "dispose"):
        raise Refusal("static lane state coordinates only deploy or dispose")


def observation(operation):
    material = operation["outcome"]["material"]
    if material["state"] == "absent":
        return {"state": "absent"}
    return {"state": "present", "revision": operation["request"]["context"]["revision"], "digest": material["artifact"]["digest"]}


def checked(value, registered):
    version = value.get("schema") if isinstance(value, dict) else None
    fields = FIELDS + (("recovery",) if version == "wharf.lane.current/v2" else ())
    value = documents.object_value(value, fields)
    initial_value = initial(registered)
    if version not in ("wharf.lane.current/v1", "wharf.lane.current/v2") or any(value[key] != initial_value[key] for key in ("target", "registration")):
        raise Refusal("lane state schema, target or registration disagrees")
    value["target"] = codec.read("address", value["target"])
    value["revision"] = shape.revision(value["revision"])
    value["phase"] = shape.one_of(value["phase"], ("vacant", "reserved", "unknown", "settled"), "lane phase")
    value["observed"] = codec.read("state", value["observed"])
    if value["observed"]["state"] == "unknown":
        raise Refusal("uncertainty belongs to the reservation, not settled lane material")
    if value["active"] is not None:
        value["active"] = reservation(value["active"])
        matches(value["active"], value, registered)
        requested = value["active"]["request"]
        if requested["context"]["revision"] != value["revision"] or requested["expected"] != value["observed"]:
            raise Refusal("lane active revision or expected state disagrees")
        if value["active"]["assessment"]["conditions"]["observed"] != value["observed"]:
            raise Refusal("lane active assessment differs from settled state")
    if (value["active"] is not None) != (value["phase"] in ("reserved", "unknown")):
        raise Refusal("lane phase and reservation disagree")
    if value["last"] is not None:
        value["last"] = codec.read("operation", value["last"])
        history(value, registered)
    elif value["revision"] != 1 or value["observed"] != {"state": "absent"} or value["phase"] not in ("vacant", "reserved"):
        raise Refusal("lane history cannot be erased into an initial state")
    if version == "wharf.lane.current/v2":
        adopted(value, registered)
    return value


def adopted(value, registered):
    receipt = recovery.checked(value["recovery"])
    before = checked(receipt["before"], registered)
    if receipt["unknown"]["reference"]["authority"] != registered["declaration"]["authorities"]["publication"]:
        raise Refusal("lane operator recovery changed its publication authority")
    matches(receipt["operation"], before, registered)
    revision = before["revision"] + 1
    if value["revision"] < revision:
        raise Refusal("lane operator recovery cannot reset its revision")
    if value["revision"] == revision and (value["last"] != receipt["operation"] or value["observed"] != observation(receipt["operation"])):
        raise Refusal("lane operator recovery differs from adopted material")


def history(value, registered):
    last = value["last"]
    matches(last, value, registered)
    if last["outcome"]["outcome"] == "applied":
        if last["request"]["context"]["revision"] + 1 != value["revision"] or observation(last) != value["observed"]:
            raise Refusal("lane settled history disagrees with revision or material")
        if value["phase"] not in ("settled", "reserved"):
            raise Refusal("lane applied history cannot become vacant or unknown")
    elif value["active"] != {key: last[key] for key in ("request", "assessment")} or value["phase"] != "unknown":
        raise Refusal("non-applied lane result must retain its exact reservation")


def started(current, value, registered):
    current = checked(current, registered)
    value = reservation(value)
    matches(value, current, registered)
    if current["active"] is not None:
        if current["active"] == value:
            return current
        raise Refusal("lane reservation is busy; no expiry or takeover")
    if current["last"] is not None and {key: current["last"][key] for key in ("request", "assessment")} == value:
        return current
    requested = value["request"]
    if current["revision"] != requested["context"]["revision"]:
        raise Refusal("lane request revision is stale")
    if current["revision"] == 18446744073709551615:
        raise Refusal("lane revision cannot advance beyond its bounded contract")
    if requested["expected"] != current["observed"] or value["assessment"]["conditions"]["observed"] != current["observed"]:
        raise Refusal("lane request and assessment disagree with settled material")
    return checked(dict(current, phase="reserved", active=value), registered)


def finished(current, operation, registered):
    current = checked(current, registered)
    operation = codec.read("operation", operation)
    matches(operation, current, registered)
    if current["active"] is None and current["last"] == operation:
        return current
    if current["active"] != {key: operation[key] for key in ("request", "assessment")}:
        raise Refusal("lane result has no exact active reservation")
    proposed = dict(current, phase="unknown", last=operation)
    if operation["outcome"]["outcome"] == "applied":
        proposed.update(phase="settled", active=None, revision=current["revision"] + 1, observed=observation(operation))
    return checked(proposed, registered)


def read(bucket, registered):
    registered = registration.checked(registered)
    body, etag = documents.snapshot(bucket, documents.location(registered["declaration"]["target"]) + "/current.json")
    return (initial(registered) if body is None else checked(documents.decode(body), registered)), etag


def reserve(bucket, requested, assessed):
    requested = codec.read("request", requested)
    registered, _ = registration.read(bucket, requested["context"]["requested"])
    value = reservation({"request": requested, "assessment": assessed})
    current, etag = read(bucket, registered)
    proposed = started(current, value, registered)
    prefix = documents.location(current["target"])
    registration.confirm(bucket, registered)
    documents.immutable(bucket, f"{prefix}/requests/{requested['context']['request']}/intent.json", documents.encode(value))
    if proposed != current:
        registration.confirm(bucket, registered)
        bucket.swap(prefix + "/current.json", documents.encode(proposed), etag)
    return proposed


def record(bucket, operation):
    operation = codec.read("operation", operation)
    registered, _ = registration.read(bucket, operation["request"]["context"]["requested"])
    current, etag = read(bucket, registered)
    proposed = finished(current, operation, registered)
    prefix = documents.location(current["target"])
    registration.confirm(bucket, registered)
    documents.immutable(bucket, f"{prefix}/requests/{operation['request']['context']['request']}/{codec.digest('operation', operation)}.json", codec.encode("operation", operation))
    if proposed != current:
        registration.confirm(bucket, registered)
        bucket.swap(prefix + "/current.json", documents.encode(proposed), etag)
    return proposed



def recovered(registered, receipt):
    if not registered["enabled"]:
        raise Refusal("lane operator recovery requires enabled registration")
    receipt = recovery.checked(receipt)
    before = checked(receipt["before"], registered)
    proposed = finished(before, receipt["operation"], registered)
    return checked(dict(proposed, schema="wharf.lane.current/v2", recovery=receipt), registered)


def recover(bucket, registered, receipt):
    registered = registration.checked(registered)
    receipt = recovery.checked(receipt)
    proposed = recovered(registered, receipt)
    prefix = documents.location(proposed["target"])
    journal = prefix + "/recovery/" + canonical.digest(receipt) + ".json"
    body, etag = documents.snapshot(bucket, prefix + "/current.json")
    if body is None:
        raise Refusal("lane operator recovery cannot invent missing current state")
    current = checked(documents.decode(body), registered)
    if current == proposed:
        if bucket.get(journal) != documents.encode(receipt):
            raise Refusal("lane operator recovery completion lacks its exact journal")
        registration.confirm(bucket, registered)
        return proposed
    expected = receipt["precondition"]
    if current != receipt["before"] or hashlib.sha256(body).hexdigest() != expected["digest"] or etag != expected["etag"]:
        raise Refusal("lane operator recovery original state or version moved")
    registration.confirm(bucket, registered)
    bucket.authorize(receipt)
    documents.immutable(bucket, journal, documents.encode(receipt))
    registration.confirm(bucket, registered)
    bucket.authorize(receipt)
    body, version = documents.snapshot(bucket, prefix + "/current.json")
    if body is None or documents.decode(body) != current or version != etag:
        raise Refusal("lane operator recovery state moved before conditional write")
    bucket.swap(prefix + "/current.json", documents.encode(proposed), etag)
    actual, _ = read(bucket, registered)
    if actual != proposed or bucket.get(journal) != documents.encode(receipt):
        raise Refusal("lane operator recovery readback is unverified; no replay")
    registration.confirm(bucket, registered)
    return actual
