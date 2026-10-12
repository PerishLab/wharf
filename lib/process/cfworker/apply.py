from lib.content import canonical
from lib.content.lane import codec, shape
from lib.content.static import evidence
from lib.process.cfworker import mapping
from lib.refusal import Refusal
from lib.store import handoff
from lib.store.lane import documents, registration, state

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


def checked(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("registration", "current", "workload", "artifact"))
    registered = registration.checked(value["registration"])
    selected = mapping.selected(registered)
    current = state.checked(value["current"], registered)
    if current["phase"] != "reserved" or current["active"] is None:
        raise Refusal("cfworker apply payload needs a fresh exact reservation; no recovery write")
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
