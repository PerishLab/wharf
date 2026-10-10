import re

from lib.refusal import Refusal


def atom(value):
    if not isinstance(value, str) or len(value) > 48 or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise Refusal("lane atom must be bounded lowercase ASCII")
    return value


def name(value):
    if not isinstance(value, str) or len(value.split(".")) > 2:
        raise Refusal("lane name requires a family and optional key")
    for part in value.split("."):
        atom(part)
    return value


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_-]{1,100}", value):
        raise Refusal("lane repository requires an owner and repository")
    return value


def hex_value(value, size=64):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{size}}}", value):
        raise Refusal("lane digest or source identity is malformed")
    return value
