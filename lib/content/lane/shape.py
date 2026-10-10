import hashlib
import json

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


def tagged(value, field, choices, family):
    if not isinstance(value, dict):
        raise Refusal(f"lane {family} requires an object")
    tag = one_of(value.get(field), choices, family)
    return object_value(value, family + "-" + tag), tag


def revision(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 18446744073709551615:
        raise Refusal("lane revision must be bounded u64")
    return value


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()
