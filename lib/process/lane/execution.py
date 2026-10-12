import os

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import evidence
from lib.content.static.github import GitHub, Routing
from lib.process.lane import entry, reservation
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents, registration, state


def location(intent):
    return documents.location(intent["target"]) + "/requests/" + intent["request"] + "/custody.json"


class Reader(entry.Reader):
    def __init__(self, intent, environ):
        super().__init__(intent, environ)
        if intent["operation"] != "inspect":
            self._keys.add(location(intent))

    def get(self, key):
        body, _ = self.snapshot(key)
        if body is None:
            raise Refusal("lane execution requires recorded original-run custody")
        return body


def checked(intent, value):
    intent = entry.requested(documents.encode(intent).decode())
    value = documents.object_value(documents.decode(documents.encode(value)), ("registration", "current", "request", "assessment", "custody"))
    registered = registration.checked(value["registration"])
    current = state.checked(value["current"], registered)
    requested = codec.read("request", value["request"])
    assessed = codec.read("assessment", value["assessment"])
    artifact = requested["intent"].get("artifact")
    if requested != reservation.requested(intent, artifact) or registration.digest(registered) != intent["registration"] or not registered["enabled"]:
        raise Refusal("lane execution projection differs from the exact dispatch or registration")
    if intent["source"] is not None and intent["source"]["declaration"] != registered["generation"]:
        raise Refusal("lane execution source configuration generation moved")
    registration.admitted(registered, requested, assessed)
    if current["revision"] != intent["revision"] or current["observed"] != intent["expected"] or assessed["conditions"]["observed"] != current["observed"]:
        raise Refusal("lane execution revision or settled material differs")
    if intent["operation"] == "inspect":
        if current["active"] is not None or value["custody"] is not None:
            raise Refusal("lane inspection requires idle state without writer custody")
    elif current["phase"] != "reserved" or current["active"] != {"request": requested, "assessment": assessed}:
        raise Refusal("lane execution needs its fresh exact reservation; no unknown continuation write")
    return dict(value, registration=registered, current=current, request=requested, assessment=assessed)


def confirm(intent, value, bucket, context):
    registered, _ = registration.read(bucket, intent["target"])
    current, _ = state.read(bucket, registered)
    if registered != value["registration"] or current != value["current"]:
        raise Refusal("lane execution registration or exact current reservation moved")
    if intent["operation"] != "inspect":
        expected = reservation.custody(value["request"], context)
        if value["custody"] != expected or bucket.get(location(intent)) != documents.encode(expected):
            raise Refusal("lane execution requires independent exact original run/attempt custody")


def admit(intent, value, bucket, trusted):
    value = checked(intent, value)
    context, api = trusted
    confirm(intent, value, bucket, context)
    authority = entry.authority(intent, context, api)
    confirm(intent, value, bucket, context)
    return dict(authority, schema="wharf.lane.execution/v1", intent=canonical.digest(intent), prepared=value)


def observe(intent, value, environ=os.environ):
    context = entry.platform(environ)
    reader = Reader(intent, environ)
    control = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    product = GitHub(r2.secret(environ, "WHARF_PREVIEW_PRODUCT_TOKEN"))
    api = Routing(intent["target"]["repository"], control, product)
    return admit(intent, evidence.decode(value) if isinstance(value, str) else value, reader, (context, api))
