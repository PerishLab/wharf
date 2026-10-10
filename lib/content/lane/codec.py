import hashlib
import json

from lib.content.lane import assessment, identity, intent, material, operation, selection, shape
from lib.refusal import Refusal

READERS = {
    "name": identity.name, "digest": identity.hex_value, "address": material.address,
    "source": material.source, "build": material.build, "artifact": material.artifact,
    "capabilities": material.capabilities, "authorities": material.authorities,
    "declaration": material.declaration, "context": selection.context,
    "selection": selection.selection, "record": operation.record, "reference": selection.reference,
    "state": assessment.state, "authorization": assessment.authorization, "writer": assessment.writer,
    "conditions": assessment.conditions, "assessment": assessment.assessment,
    "intent": intent.intent, "request": intent.request, "material": operation.material,
    "outcome": operation.outcome, "operation": operation.operation,
}


def read(kind, value):
    if not isinstance(kind, str) or kind not in READERS:
        raise Refusal("unsupported lane codec kind")
    return READERS[kind](value)


def encode(kind, value):
    return shape.encode(read(kind, value))


def digest(kind, value):
    return shape.digest(read(kind, value))


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refusal("lane JSON cannot repeat fields")
        value[key] = item
    return value


def load(kind, content):
    if not isinstance(content, (bytes, str)):
        raise Refusal("lane JSON requires UTF-8 bytes or text")
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        value = json.loads(content, object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise Refusal("lane JSON is malformed") from error
    return read(kind, value)


def verify(expected, observed, content):
    expected, observed = selection.reference(expected), selection.reference(observed)
    if expected != observed or not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != expected["digest"]:
        raise Refusal("lane reference does not match the supplied readback")
    record = load("record", content)
    if encode("record", record) != content:
        raise Refusal("lane readback is not the exact canonical record")
    return record


def admit(requested, assessed, declared=None):
    if declared is not None:
        return intent.declared(declared, requested, assessed)
    return intent.admitted(requested, assessed)


def settle(requested, expected, observed, content):
    requested = intent.request(requested)
    entry = verify(expected, observed, content)["entry"]
    if entry["kind"] != "operation" or entry["value"]["request"] != requested or entry["value"]["outcome"]["outcome"] != "applied":
        raise Refusal("lane completion needs an applied observation of the exact request")
    return entry["value"]
