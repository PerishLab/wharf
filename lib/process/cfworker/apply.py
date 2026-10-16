from lib.content import canonical
from lib.content.lane import codec, shape
from lib.content.static import evidence
from lib.process.cfworker import discard, inventory, mapping
from lib.process.domains import context, routing
from lib.refusal import Refusal
from lib.store import handoff
from lib.store.lane import documents, publication, registration, state

BUILD = tuple(shape.CONTRACT["fields"]["build"])


def content(document, bodies):
    try:
        document = evidence.decode(canonical.encode(document))
    except (TypeError, ValueError, RecursionError) as error:
        raise Refusal("cfworker apply needs a bounded immutable handoff document") from error
    files = handoff.verify(document, bodies)
    if any(not isinstance(body, bytes) for body in files.values()):
        raise Refusal("cfworker apply requires immutable byte bodies")
    return document, files


def identity(document):
    document = documents.object_value(document, ("schema", "key", "basis", "content", "files"))
    basis = documents.object_value(document["basis"], ("entry", "receipt"))
    receipt = handoff.recipe(basis["receipt"])
    source = {key: receipt["source"][key] for key in ("repository", "commit", "tree")}
    world = canonical.digest({key: receipt[key] for key in ("tools", "implementation")})
    return codec.read("artifact", {"digest": document["content"], "source": source,
                                   "build": dict(zip(BUILD, (document["key"], world)))})


def readable(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("registration", "current", "workload", "artifact"))
    registered = registration.checked(value["registration"])
    selected = mapping.selected(registered)
    current = state.checked(value["current"], registered)
    if current["phase"] not in ("reserved", "unknown") or current["active"] is None:
        raise Refusal("cfworker apply observation needs an exact active reservation")
    active = current["active"]
    registration.admitted(registered, active["request"], active["assessment"])
    requested = active["request"]
    artifact = codec.read("artifact", value["artifact"])
    workload = codec.read("digest", value["workload"])
    if requested["intent"]["action"] != "deploy" or requested["intent"].get("artifact") != artifact:
        raise Refusal("cfworker apply material differs from its exact deploy intent")
    if artifact["source"]["repository"] != selected["target"]["repository"] or artifact["build"] is None or artifact["build"][BUILD[0]] != workload:
        raise Refusal("cfworker apply needs source-bound exact workload and build identity")
    return {"registration": registered, "current": current, "workload": workload, "artifact": artifact}


def checked(value):
    value = readable(value)
    if value["current"]["phase"] != "reserved":
        raise Refusal("cfworker apply payload needs a fresh exact reservation; no recovery write")
    return value


def matches(plan, document):
    receipt = document["basis"]["receipt"]
    registered = plan["registration"]
    if plan["workload"] != document["key"] or plan["artifact"] != identity(document):
        raise Refusal("cfworker apply handoff differs from its exact material and workload")
    if receipt["app"] != registered["declaration"]["target"]["app"] or receipt["source"]["declaration"] != registered["generation"]:
        raise Refusal("cfworker apply source app or configuration generation differs from registration")


def prepare(registered, current, document, bodies):
    document, _ = content(document, bodies)
    plan = checked({"registration": registered, "current": current, "workload": document["key"], "artifact": identity(document)})
    matches(plan, document)
    return plan


def qualified(plan, document, bodies):
    plan = checked(plan)
    document, files = content(document, bodies)
    matches(plan, document)
    return plan, files


def readable_content(plan, document, bodies):
    plan = readable(plan)
    document, files = content(document, bodies)
    matches(plan, document)
    return plan, files


def deployment_status(value, selected, policy):
    value = documents.object_value(documents.decode(documents.encode(value)), ("parent", "previews", "preview", "deployments"))
    held = discard.status({key: value[key] for key in ("parent", "previews", "preview")}, selected, policy)
    preview = held["preview"]
    if preview is None or preview["id"] in {item["name"] for item in held["previews"]}:
        raise Refusal("cfworker deployment needs a present unambiguous immutable Preview")
    return dict(held, deployments=inventory.history(value["deployments"], preview))


def deployment_prepare(plan, observed, policy):
    return deployment_checked({"apply": plan, "before": observed, "policy": policy})


def deployment_checked(value):
    value = deployment_readable(value)
    checked(value["apply"])
    return value


def deployment_readable(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("apply", "before", "policy"))
    plan = readable(value["apply"])
    policy = context.prepared(value["policy"], mapping.selected(plan["registration"]))
    before = deployment_status(value["before"], mapping.selected(plan["registration"]), policy)
    if len(before["deployments"]) >= 99:
        raise Refusal("cfworker deployment needs quota headroom; no automatic retirement")
    if (plan["current"]["observed"]["state"] == "absent") != (not before["deployments"]):
        raise Refusal("cfworker retained deployments disagree with reserved settled presence")
    return {"apply": plan, "before": before, "policy": policy}


def deployment_confirm(plan, observed, deployed):
    plan = deployment_readable(plan)
    before = plan["before"]
    deployed = inventory.history([deployed], before["preview"])[0]
    if deployed in before["deployments"]:
        raise Refusal("cfworker deployment acknowledgement repeats an existing identity")
    after = deployment_status(observed, mapping.selected(plan["apply"]["registration"]), plan["policy"])
    expected = dict(before, deployments=inventory.history(before["deployments"] + [deployed], before["preview"]))
    if after != expected:
        raise Refusal("cfworker deployment readback changed identities or retired retained deployments")
    return dict(deployed)


def resume(plan, current):
    plan = deployment_readable(plan)
    original = plan["apply"]["current"]
    current = state.checked(documents.decode(documents.encode(current)), plan["apply"]["registration"])
    if current["phase"] != "unknown" or current["last"]["outcome"]["outcome"] != "unknown":
        raise Refusal("cfworker read-only recovery needs an unknown exact original request")
    if any(current[key] != original[key] for key in ("target", "registration", "revision", "active", "observed")):
        raise Refusal("cfworker recovery changed the original reservation or settled material")
    return deployment_readable(dict(plan, apply=dict(plan["apply"], current=current)))


def candidate(plan, observed):
    plan = deployment_readable(plan)
    before = plan["before"]
    after = deployment_status(observed, mapping.selected(plan["apply"]["registration"]), plan["policy"])
    retained = {item["id"] for item in before["deployments"]}
    new = [item for item in after["deployments"] if item["id"] not in retained]
    if len(new) != 1:
        raise Refusal("cfworker recovery needs exactly one new retained deployment; no latest inference")
    return deployment_confirm(plan, after, new[0])


def readback(plan, observed, value):
    plan = deployment_readable(plan)
    value = documents.object_value(documents.decode(documents.encode(value)), ("request", "artifact", "deployment", "routing", "address", "files"))
    deployed = deployment_confirm(plan, observed, value["deployment"])
    active = plan["apply"]["current"]["active"]
    if value["request"] != codec.digest("request", active["request"]) or codec.read("artifact", value["artifact"]) != plan["apply"]["artifact"]:
        raise Refusal("cfworker content readback differs from the exact request or artifact")
    if value["address"] != routing.checked(value["routing"], observed, deployed) or type(value["files"]) is not int or not 2 <= value["files"] <= 1001:
        raise Refusal("cfworker content readback differs from exact immutable routing or bounded files")
    return value


def settle(plan, observed, proof, writer):
    plan = deployment_readable(plan)
    if codec.read("writer", writer) != "idle":
        raise Refusal("cfworker apply cannot settle without original-writer quiescence")
    readback(plan, observed, proof)
    active = plan["apply"]["current"]["active"]
    return codec.read("operation", dict(active, outcome={"outcome": "applied", "material": {"state": "present", "artifact": plan["apply"]["artifact"]}, "writer": "idle"}))


def unknown(plan, reason):
    plan = deployment_readable(plan)
    active = plan["apply"]["current"]["active"]
    return codec.read("operation", dict(active, outcome={"outcome": "unknown", "reason": reason}))


def published(plan, reader, reference):
    plan = deployment_readable(plan)
    active = plan["apply"]["current"]["active"]
    operation = publication.settle(reader, plan["apply"]["registration"]["declaration"], reference, active["request"])
    if operation["assessment"] != active["assessment"]:
        raise Refusal("cfworker public outcome changed the exact original reservation assessment")
    return operation
