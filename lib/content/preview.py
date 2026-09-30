import re
from urllib.parse import urlsplit

from lib.content import canonical
from lib.refusal import Refusal

REPOSITORY = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
HEX = {size: re.compile(rf"[0-9a-f]{{{size}}}") for size in (32, 40, 64)}
REQUEST = {"schema", "operation", "repository", "app", "name", "revision", "request", "source", "registration", "caller", "workflow"}
REGISTRATION = {"schema", "repository", "app", "account", "worker", "parent", "access", "generation", "enabled"}
RESULT = {"schema", "request", "intent", "outcome", "provider", "content", "failure"}
DEPLOYMENT = {"id", "request", "digest", "latest_url", "exact_url"}


def shape(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise Refusal(f"preview {name} requires exactly {sorted(fields)}")


def text(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise Refusal(f"preview {name} must be nonempty bounded text")


def matches(value, pattern, name):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refusal(f"preview {name} is malformed")


def slug(value, name):
    matches(value, SLUG, name)
    if len(value) > 48:
        raise Refusal(f"preview {name} exceeds 48 characters")


def schema(value, name):
    if value != f"wharf.preview.{name}/v1":
        raise Refusal(f"unsupported preview {name} schema")


def environment(value):
    matches(value["repository"], REPOSITORY, "repository")
    slug(value["app"], "app")
    slug(value["name"], "name")
    return {key: value[key] for key in ("repository", "app", "name")}


def source(value):
    shape(value, {"commit", "tree", "declaration"}, "source")
    for key in ("commit", "tree"):
        matches(value[key], HEX[40], key)
    matches(value["declaration"], HEX[64], "declaration digest")


def request(value):
    shape(value, REQUEST, "request")
    schema(value["schema"], "request")
    environment(value)
    if value["operation"] not in ("apply", "inspect", "discard"):
        raise Refusal("preview operation must be apply, inspect or discard")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise Refusal("preview revision must be a nonnegative integer")
    matches(value["request"], HEX[32], "request identity")
    matches(value["registration"], HEX[64], "registration digest")
    for key in ("caller", "workflow"):
        text(value[key], key)
    if value["operation"] == "apply":
        source(value["source"])
    elif value["source"] is not None:
        raise Refusal("preview inspect and discard must not require source")
    return value


def registration(value):
    shape(value, REGISTRATION, "registration")
    schema(value["schema"], "registration")
    matches(value["repository"], REPOSITORY, "repository")
    slug(value["app"], "app")
    slug(value["worker"], "worker")
    matches(value["account"], HEX[32], "account")
    matches(value["generation"], HEX[32], "registration generation")
    text(value["parent"], "parent identity")
    if value["access"] != "public" or value["enabled"] is not True:
        raise Refusal("preview registration must explicitly enable public non-production access")
    return value


def admitted(intent, target):
    request(intent)
    registration(target)
    if any(intent[key] != target[key] for key in ("repository", "app")) or intent["registration"] != canonical.digest(target):
        raise Refusal("preview request does not match its exact registered target")


def deployment(value):
    shape(value, DEPLOYMENT, "deployment")
    text(value["id"], "deployment identity")
    matches(value["request"], HEX[32], "deployment request")
    matches(value["digest"], HEX[64], "content digest")
    for key in ("latest_url", "exact_url"):
        text(value[key], key, 512)
        try:
            url = urlsplit(value[key])
        except ValueError as error:
            raise Refusal(f"preview {key} is malformed") from error
        if url.scheme != "https" or not url.hostname or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", url.hostname) or url.netloc != url.hostname or url.query or url.fragment or url.path not in ("", "/"):
            raise Refusal(f"preview {key} must be a native HTTPS workers.dev origin")
    if value["latest_url"] == value["exact_url"]:
        raise Refusal("preview latest and exact URLs must be distinct")


def result(value, intent):
    request(intent)
    shape(value, RESULT, "result")
    schema(value["schema"], "result")
    if value["request"] != intent["request"] or value["intent"] != canonical.digest(intent):
        raise Refusal("preview result is not bound to this exact request")
    if value["outcome"] not in ("verified", "degraded", "failed", "discarded", "unknown"):
        raise Refusal("preview result outcome is unsupported")
    provider, content = value["provider"], value["content"]
    shape(provider, {"state", "deployment", "quiescent"}, "provider observation")
    shape(content, {"state", "digest"}, "content observation")
    if provider["state"] not in ("present", "absent", "unchanged", "unknown") or type(provider["quiescent"]) is not bool:
        raise Refusal("preview provider observation is malformed")
    if content["state"] not in ("verified", "unverified", "gone", "unknown"):
        raise Refusal("preview content observation is malformed")
    if provider["deployment"] is not None:
        deployment(provider["deployment"])
    if (provider["state"] == "present") != (provider["deployment"] is not None):
        raise Refusal("preview present observation requires an exact deployment")
    if content["digest"] is not None:
        matches(content["digest"], HEX[64], "observed content digest")
    if content["state"] == "verified" and content["digest"] is None:
        raise Refusal("preview verified content must carry its byte digest")
    if content["state"] == "gone" and content["digest"] is not None:
        raise Refusal("preview gone content cannot carry a current byte digest")
    if value["failure"] is not None:
        text(value["failure"], "failure", 2048)
    settled(value, intent)
    return value


def settled(value, intent):
    outcome, provider, content = value["outcome"], value["provider"], value["content"]
    if outcome != "unknown" and (not provider["quiescent"] or provider["state"] == "unknown"):
        raise Refusal("preview uncertain writer/provider cannot release the active request")
    if outcome == "verified":
        observed = provider["deployment"]
        if intent["operation"] != "apply" or provider["state"] != "present" or content["state"] != "verified" or content["digest"] != observed["digest"] or observed["request"] != intent["request"] or value["failure"] is not None:
            raise Refusal("preview verified result needs this deployment and matching byte readback")
    elif outcome == "discarded":
        if intent["operation"] != "discard" or provider["state"] != "absent" or content != {"state": "gone", "digest": None} or value["failure"] is not None:
            raise Refusal("preview discard requires provider absence and URL disappearance")
    elif outcome == "degraded":
        if intent["operation"] != "apply" or provider["state"] != "present" or content["state"] != "unverified" or provider["deployment"]["request"] != intent["request"]:
            raise Refusal("preview degraded result must identify the new unverified deployment")
    if outcome in ("degraded", "failed", "unknown") and value["failure"] is None:
        raise Refusal("preview incomplete result must explain its failure")
