import base64
import hashlib

from lib.content import resources
from lib.process.cfworker import observation
from lib.refusal import Refusal
from lib.store.lane import documents


def material():
    return resources.read_json("identity/base.json")


def prepared(pins):
    pins = documents.object_value(documents.decode(documents.encode(pins)), ("version", "deployment", "source"))
    if any(not isinstance(pins[key], str) or not observation.DEPLOYMENT.fullmatch(pins[key]) for key in ("version", "deployment")):
        raise Refusal("cfworker base requires exact version and deployment UUIDs")
    if pins["source"] != hashlib.sha256(material()["source"].encode()).hexdigest():
        raise Refusal("cfworker base source differs from the fixed inert module")
    return pins


def version(pins, value):
    pins = prepared(pins)
    value = documents.decode(documents.encode(value))
    held = material()
    module = {"name": held["main_module"], "content_type": "application/javascript+module",
              "content_base64": base64.b64encode(held["source"].encode()).decode("ascii")}
    if not isinstance(value, dict) or value.get("id") != pins["version"] or value.get("modules") != [module]:
        raise Refusal("cfworker base version differs from exact pinned inert module bytes")
    if any(value.get(key) != held[key] for key in ("main_module", "compatibility_date", "compatibility_flags")):
        raise Refusal("cfworker base runtime configuration differs from fixed material")
    if value.get("bindings") != [] or value.get("assets") is not None or value.get("tail_consumers", []) != []:
        raise Refusal("cfworker base cannot hold bindings, assets or tail consumers")
    return pins


def deployment(pins, value):
    pins = prepared(pins)
    value = documents.decode(documents.encode(value))
    if not isinstance(value, dict) or value.get("id") != pins["deployment"] or value.get("strategy") != "percentage":
        raise Refusal("cfworker base deployment differs from its exact pinned identity")
    versions = value.get("versions")
    if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], dict) or set(versions[0]) != {"version_id", "percentage"}:
        raise Refusal("cfworker base requires exactly one version without traffic splitting")
    held = versions[0]
    if held["version_id"] != pins["version"] or type(held["percentage"]) not in (int, float) or held["percentage"] != 100:
        raise Refusal("cfworker base deployment differs from its pinned version at full traffic")
    return pins


def qualify(pins, value):
    pins = prepared(pins)
    value = documents.object_value(documents.decode(documents.encode(value)), ("version", "deployment", "current"))
    current = documents.object_value(value["current"], ("deployments",))["deployments"]
    if not isinstance(current, list) or not 1 <= len(current) <= 100:
        raise Refusal("cfworker base needs a bounded nonempty current deployment catalog")
    version(pins, value["version"])
    deployment(pins, value["deployment"])
    deployment(pins, current[0])
    return pins


def isolated(pins, value):
    value = documents.object_value(documents.decode(documents.encode(value)),
                                   ("version", "deployment", "current", "routes", "environment_routes", "schedules"))
    if value["routes"] != [] or value["environment_routes"] != [] or value["schedules"] != {"schedules": []}:
        raise Refusal("cfworker inert base requires empty independently read associated routes and schedules")
    return qualify(pins, {key: value[key] for key in ("version", "deployment", "current")})

