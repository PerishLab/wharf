import re

from lib.refusal import Refusal


IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
DEPLOYMENT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
REFERENCES = {"dispatch_namespace_outbounds", "domains", "durable_objects", "queues", "workers"}


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise Refusal("cfworker returned an unsupported resource identity")
    return value


def prepared(policy):
    if not isinstance(policy, dict) or set(policy) != {"id", "subdomain"}:
        raise Refusal("cfworker parent needs prepared identity and subdomain policy")
    expected = identity(policy["id"])
    subdomain = policy["subdomain"]
    if not isinstance(subdomain, str) or not LABEL.fullmatch(subdomain):
        raise Refusal("cfworker parent policy has an unsupported subdomain")
    return {"id": expected, "subdomain": subdomain}


def parent(value, selected, policy):
    policy = prepared(policy)
    expected = policy["id"]
    subdomain = policy["subdomain"]
    if not isinstance(value, dict) or value.get("id") != expected or value.get("name") != selected["parent"]:
        raise Refusal("cfworker parent identity differs from prepared policy")
    suffix = "-" + selected["parent"] + "." + subdomain + ".workers.dev"
    routing = value.get("subdomain")
    if not isinstance(routing, dict) or routing.get("enabled") is not False or routing.get("previews_enabled") is not True or routing.get("preview_url_suffix") != suffix:
        raise Refusal("cfworker parent routing differs from dedicated Preview policy")
    if "deployed_on" not in value or value["deployed_on"] is not None or value.get("previews_base_config", None) not in (None, {}):
        raise Refusal("cfworker parent deployment or Preview base configuration drifted")
    references = value.get("references")
    if not isinstance(references, dict) or set(references) != REFERENCES or any(item != [] for item in references.values()):
        raise Refusal("cfworker parent references differ from dedicated Preview policy")
    return {"id": expected, "name": selected["parent"], "account": selected["account"], "suffix": suffix}


def preview(value, selected, held):
    name = selected["name"]
    if not isinstance(value, dict) or value.get("name") != name or value.get("slug") != name:
        raise Refusal("cfworker Preview identity differs from selected lane")
    if value.get("urls") != ["https://" + name + held["suffix"]]:
        raise Refusal("cfworker Preview address differs from prepared native routing")
    return {"id": identity(value.get("id")), "name": name, "parent": held["id"]}


def deployment(value, held, requested):
    if not isinstance(requested, str) or not DEPLOYMENT.fullmatch(requested):
        raise Refusal("cfworker observation needs an exact deployment identity")
    if not isinstance(value, dict) or value.get("id") != requested or value.get("preview_id") != held["id"] or value.get("preview_name") != held["name"]:
        raise Refusal("cfworker deployment identity differs from selected Preview")
    return {"id": requested, "preview": held["id"], "parent": held["parent"]}
