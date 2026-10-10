from lib.content.lane import codec, shape
from lib.refusal import Refusal
from lib.store.lane import documents, registration

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
    value = documents.object_value(value, FIELDS)
    initial_value = initial(registered)
    if any(value[key] != initial_value[key] for key in ("schema", "target", "registration")):
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
    return value


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
