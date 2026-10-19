from datetime import datetime

from lib.process.cfworker import mapping, observation
from lib.process.domains import base, identity
from lib.refusal import Refusal
from lib.store.lane import documents


def deployed(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise Refusal("cfworker domain parent requires an explicit deployed timestamp")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise Refusal("cfworker domain parent deployed timestamp is malformed") from error
    if moment.tzinfo is None:
        raise Refusal("cfworker domain parent deployed timestamp requires an explicit timezone")


def qualify(registered, pins, value):
    pins = identity.prepare(registered, pins)
    selected = mapping.selected(registered)
    value = documents.object_value(documents.decode(documents.encode(value)), ("policy", "parent", "domain", "base"))
    supplied = documents.object_value(value["policy"], ("id", "subdomain", "base"))
    policy = observation.prepared({key: supplied[key] for key in ("id", "subdomain")})
    qualified = base.isolated(supplied["base"], value["base"])
    if policy["id"] != pins["worker"]["id"]:
        raise Refusal("cfworker domain parent differs from its explicit immutable Worker pins")
    domain = identity.observe(registered, pins, value["domain"])
    raw = value["parent"]
    if not isinstance(raw, dict):
        raise Refusal("cfworker domain parent needs an independently observed object")
    deployed(raw.get("deployed_on"))
    references = raw.get("references")
    if not isinstance(references, dict) or set(references) != observation.REFERENCES:
        raise Refusal("cfworker domain parent requires its complete native reference categories")
    if references["domains"] != [pins["domain"]] or any(references[key] != [] for key in observation.REFERENCES - {"domains"}):
        raise Refusal("cfworker domain parent references differ from its one exact qualified domain")
    held = observation.parent(dict(raw, deployed_on=None, references={key: [] for key in observation.REFERENCES}), selected, policy)
    held["base"] = qualified
    return {"parent": held, "domain": domain}
