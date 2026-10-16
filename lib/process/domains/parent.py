from lib.process.cfworker import mapping, observation
from lib.process.domains import identity
from lib.refusal import Refusal
from lib.store.lane import documents


def qualify(registered, pins, value):
    pins = identity.prepare(registered, pins)
    selected = mapping.selected(registered)
    value = documents.object_value(documents.decode(documents.encode(value)), ("policy", "parent", "domain"))
    policy = observation.prepared(value["policy"])
    if policy["id"] != pins["worker"]["id"]:
        raise Refusal("cfworker domain parent differs from its explicit immutable Worker pins")
    domain = identity.observe(registered, pins, value["domain"])
    raw = value["parent"]
    if not isinstance(raw, dict):
        raise Refusal("cfworker domain parent needs an independently observed object")
    references = raw.get("references")
    if not isinstance(references, dict) or set(references) != observation.REFERENCES:
        raise Refusal("cfworker domain parent requires its complete native reference categories")
    if references["domains"] != [pins["domain"]] or any(references[key] != [] for key in observation.REFERENCES - {"domains"}):
        raise Refusal("cfworker domain parent references differ from its one exact qualified domain")
    held = observation.parent(dict(raw, references={key: [] for key in observation.REFERENCES}), selected, policy)
    return {"parent": held, "domain": domain}
