from lib.content.lane import identity as lane_identity
from lib.process.cfworker import mapping, observation
from lib.process.domains import base, identity, parent
from lib.refusal import Refusal
from lib.store.lane import documents


def prepared(policy, selected):
    policy = documents.decode(documents.encode(policy))
    if not isinstance(policy, dict) or set(policy) not in ({"id", "subdomain"}, {"id", "subdomain", "domain", "base"}):
        raise Refusal("cfworker routing requires exact explicit parent and optional domain pins")
    held = observation.prepared({key: policy[key] for key in ("id", "subdomain")})
    if "domain" in policy:
        pins = identity.bound(selected, policy["domain"])
        if pins["worker"]["id"] != held["id"]:
            raise Refusal("cfworker routing domain differs from its immutable parent")
        held["domain"] = pins
        held["base"] = base.prepared(policy["base"])
    return held


def projected(policy, selected):
    policy = prepared(policy, selected)
    held = {"id": policy["id"], "name": selected["parent"], "account": selected["account"],
            "suffix": "-" + selected["parent"] + "." + policy["subdomain"] + ".workers.dev"}
    if "domain" in policy:
        held["domain"] = policy["domain"]
        held["base"] = policy["base"]
    return held


def observed(registered, policy, value):
    policy = prepared(policy, mapping.selected(registered))
    if "domain" not in policy:
        raise Refusal("cfworker domain observation requires explicit prepared pins")
    held = parent.qualify(registered, policy["domain"], value)
    result = dict(held["parent"], domain=identity.prepare(registered, policy["domain"]))
    if result != projected(policy, mapping.selected(registered)):
        raise Refusal("cfworker domain observation differs from the complete prepared parent policy")
    return result


def restore(value, selected):
    fields = {"id", "name", "account", "suffix"}
    if isinstance(value, dict) and "domain" in value:
        fields.update(("domain", "base"))
    value = documents.object_value(value, fields)
    suffix = value["suffix"]
    start = "-" + selected["parent"] + "."
    if not isinstance(suffix, str) or not suffix.startswith(start) or not suffix.endswith(".workers.dev"):
        raise Refusal("cfworker namespace context has foreign parent routing")
    policy = {"id": value["id"], "subdomain": suffix[len(start):-len(".workers.dev")]}
    if "domain" in value:
        policy["domain"] = value["domain"]
        policy["base"] = value["base"]
    policy = prepared(policy, selected)
    if projected(policy, selected) != value:
        raise Refusal("cfworker parent projection differs from exact prepared context")
    return policy


def current(held, name):
    if "domain" not in held:
        return "https://" + name + held["suffix"]
    pins = held["domain"]
    if not isinstance(pins, dict) or not isinstance(name, str) or not observation.LABEL.fullmatch(name):
        raise Refusal("cfworker custom origin requires exact domain pins and Preview name")
    target = {"repository": lane_identity.repository(pins.get("repository")), "app": lane_identity.atom(pins.get("app"))}
    selected = {"account": held["account"], "parent": held["name"], "name": name, "target": target}
    restore(held, selected)
    return "https://" + identity.hostname(name + "." + pins["domain"]["hostname"])


def address(held, name, requested, prefix):
    native = observation.address(held, requested, prefix)
    if "domain" not in held:
        return native
    current(held, name)
    return "https://" + identity.hostname(prefix + "-" + name + "." + held["domain"]["domain"]["hostname"])


def urls(values, held, name, requested=None):
    native = "https://" + name + held["suffix"] if requested is None else observation.address(held, requested, requested[:8])
    expected = [native]
    if "domain" in held:
        expected.append(current(held, name) if requested is None else address(held, name, requested, requested[:8]))
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or len(values) != len(expected) or set(values) != set(expected):
        raise Refusal("cfworker URLs differ from exact explicitly prepared routing")


def preview(value, selected, held):
    urls(value.get("urls") if isinstance(value, dict) else None, held, selected["name"])
    return observation.preview(dict(value, urls=["https://" + selected["name"] + held["suffix"]]), selected, held)


def resource(value, selected, held):
    if isinstance(value, dict) and value.get("urls") not in (None, []):
        return preview(value, selected, held)
    return observation.resource(value, selected, held)
