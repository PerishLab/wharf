import re

from lib.process.cfworker import discard, inventory, observation
from lib.refusal import Refusal
from lib.store.lane import documents


def context(value, selected, registered):
    value = documents.object_value(value, ("parent", "previews", "preview"))
    parent = documents.object_value(value["parent"], ("id", "name", "account", "suffix"))
    suffix = parent["suffix"]
    start = "-" + registered["parent"] + "."
    if not isinstance(suffix, str) or not suffix.startswith(start) or not suffix.endswith(".workers.dev"):
        raise Refusal("cfworker namespace context has foreign parent routing")
    policy = {"id": parent["id"], "subdomain": suffix[len(start):-len(".workers.dev")]}
    status = discard.status(value, registered, policy)
    names = {item["name"] for item in status["previews"]}
    if len(status["previews"]) > inventory.SIZE or selected not in status["previews"] or status["preview"] is None or selected["id"] in names:
        raise Refusal("cfworker namespace context lacks unambiguous complete parent membership")
    return selected


def project(value, selected, parent):
    deployed = observation.endpoint(value, selected, value.get("id") if isinstance(value, dict) else None)
    prefix = deployed["id"][:8]
    if value.get("urls") != [observation.address(parent, deployed["id"], prefix)]:
        raise Refusal("cfworker deployment URL does not corroborate its canonical routing prefix")
    return {"id": deployed["id"], "prefix": prefix}


def read(api, status):
    previews = status["previews"]
    names = {item["name"] for item in previews}
    if len(previews) > inventory.SIZE or any(item["id"] in names for item in previews):
        raise Refusal("cfworker routing needs a bounded unambiguous immutable Preview namespace")
    catalogs = []
    namespace = {key: status[key] for key in ("parent", "previews", "preview")}
    for selected in previews:
        values = [project(value, selected, status["parent"]) for value in inventory.entries(api, selected, namespace)]
        catalogs.append({"preview": dict(selected), "deployments": sorted(values, key=lambda item: item["id"])})
    return catalogs


def checked(value, observed, deployed):
    value = documents.object_value(documents.decode(documents.encode(value)), ("status", "catalogs", "deployment"))
    if value["status"] != observed or value["deployment"] != deployed:
        raise Refusal("cfworker routing differs from exact independently observed history or deployment")
    previews = observed["previews"]
    catalogs = value["catalogs"]
    if not isinstance(catalogs, list) or len(previews) > inventory.SIZE or len(catalogs) != len(previews):
        raise Refusal("cfworker routing lacks the bounded complete parent namespace")
    names = {item["name"] for item in previews}
    if any(item["id"] in names for item in previews):
        raise Refusal("cfworker routing has an ambiguous Preview ID/name endpoint")
    prefixes, identities, selected = set(), set(), None
    for preview, supplied in zip(previews, catalogs):
        supplied = documents.object_value(supplied, ("preview", "deployments"))
        if supplied["preview"] != preview or not isinstance(supplied["deployments"], list):
            raise Refusal("cfworker routing catalog has a missing or foreign Preview")
        values = supplied["deployments"]
        if len(values) > inventory.SIZE:
            raise Refusal("cfworker routing catalog exceeds retained native bounds")
        for item in values:
            item = documents.object_value(item, ("id", "prefix"))
            identity, prefix = item["id"], item["prefix"]
            if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity) or not isinstance(prefix, str) or not re.fullmatch(r"[0-9a-f]{8}", prefix) or prefix != identity[:8]:
                raise Refusal("cfworker routing catalog has a malformed UUID or prefix")
            if identity in identities or prefix in prefixes or prefix in names:
                raise Refusal("cfworker routing prefix is ambiguous in its retained parent namespace")
            identities.add(identity)
            prefixes.add(prefix)
        if preview == observed["preview"]:
            selected = [{"id": item["id"], "preview": preview["id"], "parent": preview["parent"]} for item in values]
    if selected is None or inventory.history(selected, observed["preview"]) != inventory.history(observed["deployments"], observed["preview"]) or deployed not in selected:
        raise Refusal("cfworker routing selected catalog differs from complete exact deployment history")
    return observation.address(observed["parent"], deployed["id"], deployed["id"][:8])
