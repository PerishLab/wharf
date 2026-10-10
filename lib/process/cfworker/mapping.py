from lib.content.lane import codec
from lib.refusal import Refusal
from lib.store.lane import registration


def selected(value):
    value = registration.checked(value)
    if not value["enabled"]:
        raise Refusal("static provider registration is disabled")
    target = value["declaration"]["target"]
    name = target["lane"].replace(".", "-")
    parent = value["mapping"]["resource"]
    if len(name + "-" + parent) > 63:
        raise Refusal("static Preview and parent exceed one public DNS label; no truncation")
    return {"target": target, "account": value["mapping"]["account"], "parent": parent, "name": name}


def catalog(values):
    if not isinstance(values, list) or not values:
        raise Refusal("static provider mapping needs a prepared nonempty registration catalog")
    addresses = set()
    resources = set()
    result = []
    for value in values:
        held = selected(value)
        address = codec.encode("address", held["target"])
        resource = (held["account"], held["parent"], held["name"])
        if address in addresses or resource in resources:
            raise Refusal("static provider catalog repeats a logical or physical lane")
        addresses.add(address)
        resources.add(resource)
        result.append(held)
    return result
