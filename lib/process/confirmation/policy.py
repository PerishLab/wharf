from lib.content import canonical
from lib.content.lane import codec, preview
from lib.process.confirmation import history, original
from lib.process.lane import entry
from lib.refusal import Refusal
from lib.store.lane import documents, public


def document(value):
    try:
        return documents.decode(documents.encode(value))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise Refusal("lane confirmation requires a bounded JSON document") from None


def context(value):
    preview.shape(value, {"run", "attempt", "commit"}, "confirmation pinned context")
    for key in ("run", "attempt"):
        original.positive(value[key])
    preview.matches(value["commit"], preview.HEX[40], "confirmation pinned commit")
    return dict(value)


def selected(value):
    value = document(value)
    preview.shape(value, {"schema", "target", "request", "original"}, "confirmation request")
    if value["schema"] != "wharf.lane.confirmation-request/v1":
        raise Refusal("lane confirmation request schema is unsupported")
    value["target"] = codec.read("address", value["target"])
    value["request"] = codec.read("digest", value["request"])
    value["original"] = codec.read("reference", value["original"])
    return value


def requested(body):
    if not isinstance(body, str):
        raise Refusal("lane confirmation request requires bounded UTF-8 JSON text")
    try:
        return selected(documents.decode(body.encode()))
    except UnicodeError:
        raise Refusal("lane confirmation request requires valid UTF-8") from None


def pinned(value):
    value = document(value)
    preview.shape(value, {"schema", "dispatch", "original", "context", "actors", "observer", "current", "publication"}, "trusted confirmation pins")
    if value["schema"] != "wharf.lane.confirmation-pins/v1":
        raise Refusal("lane confirmation pins schema is unsupported")
    intent = entry.requested(documents.encode(value["dispatch"]).decode())
    value["dispatch"] = intent
    value["original"] = codec.read("reference", value["original"])
    value["context"] = context(value["context"])
    value["actors"] = original.actors(value["actors"])
    history.supported(intent, value["context"])
    expected = f"{entry.policy()['repository']}/{entry.policy()['workflow']}@{value['context']['commit']}"
    if intent["workflow"] != expected or intent["caller"] != value["actors"]["actor"]["login"]:
        raise Refusal("lane confirmation original dispatch differs from trusted source/actor pins")
    preview.shape(value["observer"], {"context", "actors"}, "trusted confirmation observer")
    value["observer"] = {"context": context(value["observer"]["context"]), "actors": original.actors(value["observer"]["actors"])}
    observer = value["observer"]
    if observer["context"]["run"] == value["context"]["run"] or observer["context"]["attempt"] != 1:
        raise Refusal("lane confirmation needs a distinct observer run without reruns")
    old = {user["id"] for user in value["actors"].values()}
    if any(user["id"] in old for user in observer["actors"].values()):
        raise Refusal("lane confirmation observer cannot reuse original actor identities")
    preview.shape(value["current"], {"digest", "etag"}, "trusted confirmation current pin")
    value["current"]["digest"] = codec.read("digest", value["current"]["digest"])
    etag = value["current"]["etag"]
    if not isinstance(etag, str) or not 1 <= len(etag) <= 256 or any(not 33 <= ord(char) <= 126 for char in etag):
        raise Refusal("lane confirmation needs the exact prepared current ETag")
    reader = public.prepare(value["publication"])
    if reader.authority != value["original"]["authority"]:
        raise Refusal("lane confirmation original belongs to another prepared public authority")
    return value


def checked(intent, pins):
    intent, pins = selected(intent), pinned(pins)
    dispatch = pins["dispatch"]
    if any(intent[key] != dispatch[key] for key in ("target", "request")) or intent["original"] != pins["original"]:
        raise Refusal("lane confirmation selector differs from independently prepared original scope")
    return intent, pins


def bound(value):
    return canonical.digest(documents.decode(documents.encode(value)))
