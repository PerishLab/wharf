import os

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import apply, create, reader, upload, writer
from lib.process.lane import build, disposal, entry, execution, provider
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents


def authority(before, environ):
    intent = entry.requested(documents.encode(before["dispatch"]).decode())
    if execution.observe(intent, before["execution"]["prepared"], environ) != before["execution"]:
        raise Refusal("lane deployment original-run authority or exact reservation moved")


def unchanged(before, plan, client, environ):
    observed = client.history(plan["policy"])
    if observed != plan["before"]:
        raise Refusal("lane deployment complete provider pre-write history moved")
    authority(before, environ)
    return observed


def unknown(before, plan):
    value = before["execution"]["prepared"]
    active = {key: value[key] for key in ("request", "assessment")}
    operation = codec.read("operation", dict(active, outcome={"outcome": "unknown", "reason": "unverified"}))
    return {"execution": canonical.digest(before["execution"]), "plan": plan, "operation": operation}


def prepare(held, environ):
    forbidden = tuple(name for name in build.FORBIDDEN if name != "WHARF_LANE_PROVIDER_TOKEN") + ("WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN")
    if any(r2.secret(environ, name) for name in forbidden):
        raise Refusal("lane deployment holds only its provider writer and prepared readers")
    intent = entry.requested(held["request"])
    if intent["operation"] != "apply":
        raise Refusal("lane deployment accepts only exact apply dispatch")
    executioned = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    value = executioned["prepared"]
    supplied = documents.object_value(evidence.decode(held["provider"]), ("execution", "plan", "operation"))
    if supplied["operation"] is not None or supplied["execution"] != canonical.digest(executioned):
        raise Refusal("lane deployment preflight belongs to another exact execution")
    document, bodies = provider.content(value["request"]["intent"]["artifact"], environ)
    if held["workload"] != document["key"]:
        raise Refusal("lane deployment changed its exact reserved workload selector")
    material = apply.prepare(value["registration"], value["current"], document, bodies)
    policy = provider.policy(environ, value["registration"])
    client = reader.Reader(r2.secret(environ, "WHARF_LANE_PROVIDER_READER_TOKEN"), value["registration"])
    status = client.status(policy)
    plan = create.prepare(material, status, policy) if status["preview"] is None else apply.deployment_prepare(material, client.history(policy), policy)
    if plan != supplied["plan"]:
        raise Refusal("lane deployment content, reservation or complete preflight moved")
    before = {"dispatch": intent, "execution": executioned}
    authority(before, environ)
    disposal.spent(value, environ, "apply")
    return before, plan, client, (document, bodies)


def creation(before, plan, client, trusted):
    environ, content = trusted
    document, bodies = content
    owned = create.Creator(r2.secret(environ, "WHARF_LANE_PROVIDER_TOKEN"), plan)
    observed = client.status(plan["policy"])
    authority(before, environ)
    result = owned.create(observed, document, bodies)
    if not result["acknowledged"]:
        return None
    observed = client.status(plan["policy"])
    create.confirmed(plan, observed, result["preview"])
    authority(before, environ)
    history = client.history(plan["policy"])
    if {key: history[key] for key in ("parent", "preview", "previews")} != observed:
        raise Refusal("lane deployment created identity moved before upload")
    return apply.deployment_prepare(plan["apply"], history, plan["policy"])


def uploading(before, plan, client, trusted):
    environ, content = trusted
    document, bodies = content
    owned = upload.Uploader(r2.secret(environ, "WHARF_LANE_PROVIDER_TOKEN"), plan["apply"])
    unchanged(before, plan, client, environ)
    result = owned.session(document, bodies)
    if not result["acknowledged"]:
        return None
    for number in range(result["buckets"]):
        unchanged(before, plan, client, environ)
        if not owned.bucket(number, document, bodies)["acknowledged"]:
            return None
    return owned.completion(document, bodies)


def perform(held, environ=os.environ):
    before, plan, client, content = prepare(held, environ)
    if plan["before"]["preview"] is None:
        created = creation(before, plan, client, (environ, content))
        if created is None:
            authority(before, environ)
            return unknown(before, plan)
        plan = created
    completion = uploading(before, plan, client, (environ, content))
    if completion is None:
        authority(before, environ)
        return unknown(before, plan)
    document, bodies = content
    owned = writer.Deployer(r2.secret(environ, "WHARF_LANE_PROVIDER_TOKEN"), plan, completion)
    result = owned.deploy(unchanged(before, plan, client, environ), document, bodies)
    if not result["acknowledged"]:
        authority(before, environ)
        return unknown(before, plan)
    observed = client.history(plan["policy"])
    routed = client.observe(plan["policy"], result["deployment"]["id"])
    proof = reader.Readback(plan, observed, routed).read(document, bodies)
    reader.Readback(plan, observed, routed).current().read(document, bodies)
    if client.observe(plan["policy"], result["deployment"]["id"]) != routed:
        raise Refusal("lane deployment complete routing namespace moved during content readback")
    after = client.history(plan["policy"])
    if after != observed:
        raise Refusal("lane deployment provider history moved during content readback")
    authority(before, environ)
    operation = apply.settle(plan, after, proof, "idle")
    return {"execution": canonical.digest(before["execution"]), "plan": plan, "operation": operation}
