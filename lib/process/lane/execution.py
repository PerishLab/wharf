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


def producer_names(action):
    intake = "[verify] request lane"
    return {
        intake + " / [verify] request lane": "success",
        intake + " / [build] product lane": "success" if action == "apply" else "skipped",
        intake + " / [bind] request lane": "success",
        intake + " / [verify] product lane": "success",
        "[read] cfworker lane / [verify] product lane": "success",
        "[deploy] cfworker lane / [deploy] cfworker lane": "success" if action == "apply" else "skipped",
        "[deploy] cfworker disposal / [deploy] cfworker disposal": "success" if action == "dispose" else "skipped",
        "[record] evidence lane / [record] evidence lane": "success",
    }


def native_jobs(intent, context, api):
    value = api.jobs(context["run"], context["attempt"])
    if not isinstance(value, dict) or type(value.get("total_count")) is not int or not isinstance(value.get("jobs"), list):
        raise Refusal("lane original-attempt native jobs are unavailable or malformed")
    jobs = value["jobs"]
    if value["total_count"] != len(jobs) or not 1 <= len(jobs) <= 30:
        raise Refusal("lane native job read is incomplete or exceeds its single-page budget")
    expected = producer_names(intent["operation"])
    names, identities = set(), set()
    for job in jobs:
        if not isinstance(job, dict) or type(job.get("id")) is not int or job["id"] < 1 or not isinstance(job.get("name"), str):
            raise Refusal("lane native job identity is malformed")
        name = job["name"]
        inactive = "[deploy] cfworker disposal" if intent["operation"] == "apply" else "[deploy] cfworker lane"
        if name == inactive and job.get("conclusion") == "skipped":
            name += " / " + inactive
        if intent["operation"] == "inspect" and name == "[deploy] cfworker disposal" and job.get("conclusion") == "skipped":
            name += " / [deploy] cfworker disposal"
        if name in names or job["id"] in identities:
            raise Refusal("lane native job identities or names are ambiguous")
        names.add(name)
        identities.add(job["id"])
        attempt = job.get("run_attempt", context["attempt"])
        if type(job.get("run_id")) is not int or type(attempt) is not int or job["run_id"] != context["run"] or job.get("head_sha") != context["commit"] or attempt != context["attempt"]:
            raise Refusal("lane native job belongs to another original run/attempt or commit")
        if name == "[record] evidence lane / [record] state lane":
            if job.get("status") != "in_progress" or job.get("conclusion") is not None:
                raise Refusal("lane state job must be the current native writer")
        elif name == "[verify] request lane / [verify] request lane" and job.get("status") in ("queued", "in_progress") and job.get("conclusion") is None:
            continue
        elif name not in expected or job.get("status") != "completed" or job.get("conclusion") != expected[name]:
            raise Refusal("lane native producer is absent, unsuccessful or not quiescent")
    if names != set(expected) | {"[record] evidence lane / [record] state lane"}:
        raise Refusal("lane native producers are missing; no idle writer inferred")
    return value


def quiescence(intent, environ=os.environ):
    context = entry.platform(environ)
    api = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    return native_jobs(intent, context, api)
