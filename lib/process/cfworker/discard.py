from lib.content.lane import codec
from lib.process.cfworker import inventory, mapping, observation
from lib.process.domains import context
from lib.refusal import Refusal
from lib.store.lane import documents, publication, registration, state


def status(value, selected, policy):
    value = documents.object_value(value, ("parent", "previews", "preview"))
    policy = context.prepared(policy, selected)
    parent = context.projected(policy, selected)
    if value["parent"] != parent:
        raise Refusal("cfworker discard status differs from prepared parent identity")
    values = value["previews"]
    if not isinstance(values, list) or len(values) > inventory.ITEMS:
        raise Refusal("cfworker discard needs a bounded complete status projection")
    names, identities, catalog = set(), set(), []
    for item in values:
        item = documents.object_value(item, ("id", "name", "parent"))
        name = item["name"]
        if not isinstance(name, str) or not observation.LABEL.fullmatch(name) or len(name + "-" + parent["name"]) > 63 or item["parent"] != parent["id"]:
            raise Refusal("cfworker discard catalog has a foreign Preview identity")
        identity = observation.identity(item["id"])
        if name in names or identity in identities:
            raise Refusal("cfworker discard catalog repeats a Preview identity")
        names.add(name)
        identities.add(identity)
        catalog.append({"id": identity, "name": name, "parent": parent["id"]})
    catalog.sort(key=lambda item: item["name"])
    preview = next((item for item in catalog if item["name"] == selected["name"]), None)
    if value["preview"] != preview:
        raise Refusal("cfworker discard selection differs from complete catalog")
    return {"parent": parent, "previews": catalog, "preview": None if preview is None else dict(preview)}


def prepare(registered, current, observed, policy):
    return checked({"registration": registered, "current": current, "before": observed, "policy": policy})


def checked(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("registration", "current", "before", "policy"))
    registered = registration.checked(value["registration"])
    selected = mapping.selected(registered)
    current = state.checked(value["current"], registered)
    if current["phase"] not in ("reserved", "unknown") or current["active"] is None:
        raise Refusal("cfworker discard needs an exact active reservation")
    active = current["active"]
    registration.admitted(registered, active["request"], active["assessment"])
    if active["request"]["intent"]["action"] != "dispose":
        raise Refusal("cfworker discard accepts only the exact dispose intent")
    policy = context.prepared(value["policy"], selected)
    before = status(value["before"], selected, policy)
    if before["preview"] is not None and current["observed"]["state"] == "absent":
        raise Refusal("cfworker selected presence conflicts with reserved absent material")
    return {"registration": registered, "current": current, "before": before, "policy": policy}


def settle(plan, observed, writer):
    plan = checked(plan)
    if codec.read("writer", writer) != "idle":
        raise Refusal("cfworker discard cannot settle without original-writer quiescence")
    after = status(observed, mapping.selected(plan["registration"]), plan["policy"])
    before = plan["before"]
    siblings = [item for item in before["previews"] if item != before["preview"]]
    if after["parent"] != before["parent"] or after["preview"] is not None or after["previews"] != siblings:
        raise Refusal("cfworker discard readback changed parent, siblings or selected absence")
    active = plan["current"]["active"]
    return codec.read("operation", dict(active, outcome={"outcome": "applied", "material": {"state": "absent"}, "writer": "idle"}))


def resume(plan, current):
    plan = checked(plan)
    original = plan["current"]
    current = state.checked(documents.decode(documents.encode(current)), plan["registration"])
    if current["phase"] != "unknown" or current["last"]["outcome"]["outcome"] != "unknown":
        raise Refusal("cfworker discard recovery needs the exact original unknown request")
    if any(current[key] != original[key] for key in ("target", "registration", "revision", "active", "observed")):
        raise Refusal("cfworker discard recovery changed the original reservation or material")
    return checked(dict(plan, current=current))


def unknown(plan, reason):
    plan = checked(plan)
    return codec.read("operation", dict(plan["current"]["active"], outcome={"outcome": "unknown", "reason": reason}))


def published(plan, reader, reference):
    plan = checked(plan)
    active = plan["current"]["active"]
    operation = publication.settle(reader, plan["registration"]["declaration"], reference, active["request"])
    if operation["assessment"] != active["assessment"]:
        raise Refusal("cfworker public discard changed the original reservation assessment")
    return operation
