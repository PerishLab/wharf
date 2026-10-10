from lib.content import resources
from lib.refusal import Refusal

CONTRACT = resources.read_json("identity/lane.json")


def object_value(value, kind, optional=()):
    fields = CONTRACT["fields"][kind]
    if not isinstance(value, dict) or set(value) - set(fields) or set(fields) - set(value) - set(optional):
        raise Refusal(f"lane {kind} requires declared fields only")
    return {field: value.get(field) for field in fields}


def one_of(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        raise Refusal(f"lane {name} is unsupported")
    return value
