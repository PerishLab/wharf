import os

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import apply, create, discard, observation, reader
from lib.process.lane import entry, execution, transfer
from lib.refusal import Refusal
from lib.store import handoff, r2


def policy(environ):
    return observation.prepared({"id": environ.get("WHARF_LANE_PARENT_ID"), "subdomain": environ.get("WHARF_LANE_SUBDOMAIN")})


def content(artifact, environ):
    artifact = codec.read("artifact", artifact)
    if artifact["build"] is None:
        raise Refusal("lane provider observation requires saved verified build identity")
    key = artifact["build"][apply.BUILD[0]]
    document, files = handoff.fetch(transfer.Reader(key, environ), key)
    if apply.identity(document) != artifact:
        raise Refusal("lane provider content differs from the full saved artifact identity")
    bodies = {item["blob"]: files[name] for name, item in document["files"].items()}
    return document, bodies.__getitem__


def inspection(value, observed, prepared, environ):
    active = {key: value[key] for key in ("request", "assessment")}
    plan = reader.inspection({"registration": value["registration"], "current": value["current"], "inspection": active, "policy": prepared})
    client = reader.Inspection(plan, observed)
    document, bodies = None, None
    if value["current"]["observed"]["state"] == "present":
        artifact = value["current"]["last"]["outcome"]["material"]["artifact"]
        document, bodies = content(artifact, environ)
    return plan, client.read(document, bodies)


def observe(held, environ=os.environ):
    transfer.readonly(environ)
    intent = entry.requested(held["request"])
    before = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    value = before["prepared"]
    action = value["request"]["intent"]["action"]
    if action != "deploy" and held["workload"] != "none":
        raise Refusal("lane provider inspect/dispose consume no new build selector")
    prepared = policy(environ)
    client = reader.Reader(r2.secret(environ, "WHARF_LANE_PROVIDER_READER_TOKEN"), value["registration"])
    status = client.status(prepared)
    operation = None
    if action == "inspect":
        plan, operation = inspection(value, status, prepared, environ)
    elif action == "dispose":
        plan = discard.prepare(value["registration"], value["current"], status, prepared)
    else:
        artifact = value["request"]["intent"]["artifact"]
        if held["workload"] != artifact["build"][apply.BUILD[0]]:
            raise Refusal("lane provider observation changed the exact reserved workload")
        document, bodies = content(artifact, environ)
        applied = apply.prepare(value["registration"], value["current"], document, bodies)
        plan = create.prepare(applied, status, prepared) if status["preview"] is None else apply.deployment_prepare(applied, client.history(prepared), prepared)
    if client.status(prepared) != status:
        raise Refusal("lane provider status moved during observation; no result admitted")
    after = execution.observe(intent, value, environ)
    if after != before:
        raise Refusal("lane provider authority or exact state changed during observation")
    return {"execution": canonical.digest(after), "plan": plan, "operation": operation}
