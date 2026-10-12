import os

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.lane import entry
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents, registration, state


def requested(intent, artifact):
    intent = entry.requested(documents.encode(intent).decode())
    action = "deploy" if intent["operation"] == "apply" else intent["operation"]
    material = {"action": action}
    if action == "deploy":
        artifact = codec.read("artifact", artifact)
        expected = dict(repository=intent["target"]["repository"], **{key: intent["source"][key] for key in ("commit", "tree")})
        if artifact["source"] != expected or artifact["build"] is None:
            raise Refusal("lane deploy requires the exact source-bound recorded build")
        material["artifact"] = artifact
    elif artifact is not None:
        raise Refusal("lane inspect/dispose must not acquire new product content")
    context = {"requested": intent["target"], "request": intent["request"], "revision": intent["revision"], "adaptor": "static"}
    expected = None if action == "inspect" else intent["expected"]
    return codec.read("request", {"context": context, "intent": material, "expected": expected})


def assessment(request, observation):
    registered = registration.checked(observation["registration"])
    current = state.checked(observation["current"], registered)
    if current["active"] is not None:
        raise Refusal("lane writer custody is busy or unknown")
    reference = {"authority": registered["declaration"]["authorities"]["authorization"], "digest": canonical.digest(observation)}
    value = {"request": codec.digest("request", request), "capabilities": registered["declaration"]["capabilities"],
             "conditions": {"authorization": {"state": "granted", "evidence": reference}, "writer": "idle", "observed": current["observed"]}}
    value = codec.read("assessment", value)
    registration.admitted(registered, request, value)
    return value


def custody(request, context):
    return {"schema": "wharf.lane.custody/v1", "request": codec.digest("request", request),
            "commit": context["commit"], "run": context["run"], "attempt": context["attempt"]}


def prepare(held, environ=os.environ):
    intent = entry.requested(held["request"])
    observation = entry.observe(intent, environ)
    request = requested(intent, evidence.decode(held["artifact"]))
    assessed = assessment(request, observation)
    registered = observation["registration"]
    if request["intent"]["action"] == "inspect":
        return {"registration": registered, "current": observation["current"], "request": request, "assessment": assessed, "custody": None}
    bucket = r2.configured(environ)
    registration.confirm(bucket, registered)
    current = state.reserve(bucket, request, assessed)
    if current["phase"] != "reserved" or current["active"] != {"request": request, "assessment": assessed}:
        raise Refusal("lane mutation requires its exact fresh reservation")
    context = entry.platform(environ)
    owned = custody(request, context)
    registration.confirm(bucket, registered)
    key = documents.location(intent["target"]) + "/requests/" + intent["request"] + "/custody.json"
    bucket.create(key, documents.encode(owned))
    if bucket.get(key) != documents.encode(owned):
        raise Refusal("lane writer custody failed independent exact readback")
    return {"registration": registered, "current": current, "request": request, "assessment": assessed, "custody": owned}
