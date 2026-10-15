import re

from lib.content.lane import codec, preview
from lib.process.cfworker import mapping, observation
from lib.refusal import Refusal
from lib.store.lane import documents

KEYS = {"repository", "app", "account", "worker", "domain"}
DOMAIN = {"id", "hostname", "zone_id", "zone_name", "certificate_id"}
PREFIX = re.compile(r"[0-9a-f]{8}")


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or any(not observation.LABEL.fullmatch(label) for label in value.split(".")):
        raise Refusal("cfworker custom domain exceeds canonical DNS limits; no normalization or truncation")
    return value


def prepare(registered, pins):
    selected = mapping.selected(registered)
    pins = documents.decode(documents.encode(pins))
    preview.shape(pins, KEYS, "custom domain pins")
    preview.shape(pins["worker"], {"id", "name"}, "custom domain Worker pins")
    preview.shape(pins["domain"], DOMAIN, "custom domain identity pins")
    target = selected["target"]
    if any(pins[key] != target[key] for key in ("repository", "app")) or pins["account"] != selected["account"] or pins["worker"]["name"] != selected["parent"]:
        raise Refusal("cfworker custom domain pins differ from the exact registered app/account/parent")
    observation.identity(pins["worker"]["id"])
    held = pins["domain"]
    observation.identity(held["id"])
    preview.matches(held["zone_id"], preview.HEX[32], "custom domain zone identity")
    preview.matches(held["certificate_id"], observation.DEPLOYMENT, "custom domain certificate identity")
    if held["zone_name"] != "perish.uk" or held["hostname"] != hostname("lane." + target["app"] + ".perish.uk"):
        raise Refusal("cfworker custom domain needs the exact explicit lane app base in perish.uk")
    return pins


def observe(registered, pins, value):
    pins = prepare(registered, pins)
    if not isinstance(value, dict):
        raise Refusal("cfworker custom domain needs an independently read object")
    held = pins["domain"]
    expected = {key: held[key] for key in ("id", "hostname", "zone_id", "zone_name")}
    expected.update(cert_id=held["certificate_id"], service=pins["worker"]["name"])
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        raise Refusal("cfworker custom domain identity differs from explicit pins")
    if value.get("enabled") is not False or value.get("previews_enabled") is not True:
        raise Refusal("cfworker custom domain requires explicit Preview-only routing, never production")
    return dict(expected, enabled=False, previews_enabled=True)


def addresses(registered, pins, prefix):
    pins = prepare(registered, pins)
    preview.matches(prefix, PREFIX, "explicit custom domain deployment prefix")
    lane = codec.read("address", registered["declaration"]["target"])["lane"]
    base = pins["domain"]["hostname"]
    return {"current": "https://" + hostname(lane + "." + base),
            "deployment": "https://" + hostname(prefix + "-" + lane + "." + base)}
