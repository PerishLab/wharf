from lib.content.lane import identity, shape
from lib.refusal import Refusal


def address(value):
    value = shape.object_value(value, "address")
    return {"repository": identity.repository(value["repository"]), "app": identity.atom(value["app"]), "lane": identity.name(value["lane"])}


def source(value):
    value = shape.object_value(value, "source")
    return {"repository": identity.repository(value["repository"]), "commit": identity.hex_value(value["commit"], 40), "tree": identity.hex_value(value["tree"], 40)}


def build(value):
    value = shape.object_value(value, "build")
    return {field: identity.hex_value(digest) for field, digest in value.items()}


def artifact(value):
    value = shape.object_value(value, "artifact", ("build",))
    return {"digest": identity.hex_value(value["digest"]), "source": source(value["source"]), "build": None if value["build"] is None else build(value["build"])}


def capabilities(value):
    actions = shape.CONTRACT["actions"]
    if not isinstance(value, list) or any(not isinstance(action, str) or action not in actions for action in value):
        raise Refusal("lane capabilities require supported actions")
    if len(set(value)) != len(value):
        raise Refusal("lane capabilities cannot repeat actions")
    return [action for action in actions if action in value]


def authorities(value):
    value = shape.object_value(value, "authorities")
    return {field: identity.atom(authority) for field, authority in value.items()}


def declaration(value):
    value = shape.object_value(value, "declaration")
    return {"target": address(value["target"]), "adaptor": identity.atom(value["adaptor"]), "capabilities": capabilities(value["capabilities"]), "authorities": authorities(value["authorities"])}
