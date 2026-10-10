import re

from lib.content.lane import preview
from lib.refusal import Refusal

ACTIONS = {"inspect", "build", "deploy", "dispose"}
PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9-]*/)?[a-z0-9][a-z0-9._-]*")
RESOURCE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def repository(value):
    preview.matches(value, preview.REPOSITORY, "lane repository")
    if any(len(part) > 100 for part in value.split("/")):
        raise Refusal("lane repository segments exceed 100 characters")


def name(value):
    if not isinstance(value, str) or len(value.split(".")) > 2:
        raise Refusal("lane name must have one family and at most one key")
    for part in value.split("."):
        preview.slug(part, "lane atom")
    if value.split(".")[0] != "preview":
        raise Refusal("first static binding requires preview or preview.<key>")


def resource(value):
    preview.matches(value, RESOURCE, "Worker resource")
    if len(value) > 63:
        raise Refusal("Worker resource exceeds 63 characters")


def mapping(value):
    preview.shape(value, {"provider", "access", "account", "resource"}, "lane mapping")
    if value["provider"] != "cfworker" or value["access"] != "public":
        raise Refusal("static lane mapping requires cfworker and explicit public access")
    preview.matches(value["account"], preview.HEX[32], "lane account")
    resource(value["resource"])


def binding(value):
    preview.shape(value, {"adaptor", "capabilities", "authorities"}, "lane binding")
    if value["adaptor"] != "static":
        raise Refusal("first lane binding requires the static adaptor")
    actions = value["capabilities"]
    if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
        raise Refusal("lane capabilities must be a list of supported actions")
    if len(set(actions)) != len(actions) or not {"inspect", "deploy"} <= set(actions) <= ACTIONS:
        raise Refusal("static capabilities require inspect/deploy, optional build/dispose, without duplicates")
    preview.shape(value["authorities"], {"authorization", "publication"}, "lane authorities")
    for authority in value["authorities"].values():
        preview.slug(authority, "lane authority")


def app(value):
    preview.shape(value, {"path", "package", "mapping", "binding"}, "lane app")
    preview.matches(value["package"], PACKAGE, "lane package selector")
    mapping(value["mapping"])
    bindings = value["binding"]
    if not isinstance(bindings, dict) or not bindings:
        raise Refusal("lane app needs explicit nonempty bindings")
    for lane, declared in bindings.items():
        name(lane)
        binding(declared)


def read(document):
    if "preview" in document:
        raise Refusal("preview declarations are retired; declare app and bindings under lane")
    if "lane" not in document:
        return {}
    declared = document["lane"]
    preview.shape(declared, {"repository", "app"}, "lane declaration")
    repository(declared["repository"])
    apps = declared["app"]
    if not isinstance(apps, dict) or not apps:
        raise Refusal("lane declaration needs an explicit nonempty app table")
    for identity, value in apps.items():
        preview.slug(identity, "lane app identity")
        app(value)
    return apps
