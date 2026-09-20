import os
import re
import tomllib
from pathlib import Path

from lib.content import resources
from lib.refusal import Refusal

CATALOG = resources.read_json("parameters.json")
PREFIX = CATALOG["prefix"]
TYPES = CATALOG["types"]
DEFAULTS = CATALOG["defaults"]
SHAPES_DECLARED = {name: re.compile(shape) for name, shape in CATALOG["shapes"].items()}
CREDENTIAL = re.compile(CATALOG["credential"])
SHAPES = {"string": str, "bool": bool, "int": int, "strings": list}
LENGTH = 72
OUTPUT = "GITHUB_OUTPUT"


def declared(name):
    if name not in TYPES:
        raise Refusal(f"parameter {name} is not declared in parameters.json")
    return TYPES[name]


def variable(name):
    return f"{PREFIX}_" + name.upper().replace("-", "_")


def guarded(names):
    held = sorted(name for name in names if CREDENTIAL.search(variable(name)))
    if held:
        raise Refusal(f"credentials never pass through parameters: {', '.join(held)}")


def cast(name, value):
    kind = declared(name)
    if not value.strip():
        raise Refusal(f"parameter {name} is set to an empty value")
    if kind == "bool" and value not in ("true", "false"):
        raise Refusal(f"parameter {name} is {value!r}, which is neither true nor false")
    if kind == "int" and not re.fullmatch(r"-?[0-9]+", value):
        raise Refusal(f"parameter {name} is {value!r}, which is not an integer")
    return held(name, {"bool": lambda text: text == "true", "int": int, "strings": lines}.get(kind, str)(value))


def lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def shaped(name, value):
    kind = declared(name)
    if not isinstance(value, SHAPES[kind]) or isinstance(value, bool) != (kind == "bool"):
        raise Refusal(f"parameter {name} must be {kind}")
    if kind == "strings" and not all(isinstance(item, str) for item in value):
        raise Refusal(f"parameter {name} must be strings")
    return held(name, value)


def held(name, value):
    shape = SHAPES_DECLARED.get(name)
    if shape and not shape.fullmatch(str(value)):
        raise Refusal(f"parameter {name} is {value!r}, which is not {shape.pattern}")
    return value


def ahead(pending, token):
    if not pending:
        raise Refusal(f"{token} was given without a value")
    return pending.pop(0)


def split(token):
    if not token.startswith("--") or len(token) < 3:
        raise Refusal(f"{token!r} is not a --parameter")
    name, sign, value = token[2:].partition("=")
    return name, (value if sign else None)


def flagged(argv):
    given = {}
    path = None
    pending = list(argv)
    while pending:
        token = pending.pop(0)
        if token in ("-c", "--config"):
            path = ahead(pending, token)
            continue
        name, value = split(token)
        given.setdefault(name, []).append(supplied(name, value, pending))
    return given, path


def supplied(name, value, pending):
    if value is not None:
        return value
    if declared(name) == "bool":
        return "true"
    return ahead(pending, f"--{name}")


def once(name, values):
    if declared(name) == "strings":
        return "\n".join(values)
    if len(values) > 1:
        raise Refusal(f"parameter {name} was given {len(values)} times")
    return values[0]


def read(path):
    file = Path(path)
    if not file.is_file():
        raise Refusal(f"configuration {path} does not exist")
    try:
        held = tomllib.loads(file.read_text())
    except tomllib.TOMLDecodeError as broken:
        raise Refusal(f"configuration {path} is not TOML: {broken}") from broken
    return held


def section(held, action):
    above = {name: value for name, value in held.items() if not isinstance(value, dict)}
    guarded(above)
    below = held.get(action) or {}
    guarded(below)
    return above | below


def chosen(name, given, held, environ):
    if name in given:
        return cast(name, once(name, given[name])), "flag"
    if variable(name) in environ:
        return cast(name, environ[variable(name)]), "environment"
    if name in held:
        return shaped(name, held[name]), "configuration"
    if name in DEFAULTS:
        return (None if DEFAULTS[name] is None else shaped(name, DEFAULTS[name])), "default"
    return None, None


def unset(name, path):
    where = f"configuration {path}" if path else "no configuration was given"
    return f"{name}: not --{name}, not {variable(name)}, not in {where}, and no default"


def taken(names, given):
    unknown = sorted(set(given) - set(names))
    if unknown:
        raise Refusal(f"parameters not taken by this action: {', '.join(unknown)}")


def resolve(action, names, argv, environ=None):
    guarded(names)
    given, path = flagged(argv)
    taken(names, given)
    held = section(read(path), action) if path else {}
    found = {name: chosen(name, given, held, os.environ if environ is None else environ) for name in names}
    missing = [unset(name, path) for name, (_, origin) in sorted(found.items()) if origin is None]
    if missing:
        raise Refusal("these parameters are set nowhere: " + "; ".join(missing))
    return {name: value for name, (value, _) in found.items()}, {name: origin for name, (_, origin) in found.items()}


def shown(value):
    text = repr(value)
    return text if len(text) <= LENGTH else f"{text[:LENGTH]}... ({len(text)} characters)"


def report(values, origins):
    return [f"{name}: {shown(values[name])} ({origins[name]})" for name in sorted(origins)]


def answer(held):
    path = os.environ.get(OUTPUT)
    if not path:
        raise Refusal(f"{OUTPUT} is not set, so there is nowhere to answer the caller")
    with Path(path).open("a") as file:
        file.write("".join(f"{name}={value}\n" for name, value in sorted(held.items())))
    return held


def acted(prog, actions, argv):
    if not argv or argv[0] not in actions:
        raise Refusal(f"{prog} takes one action: {', '.join(sorted(actions))}")
    return argv[0], argv[1:]
