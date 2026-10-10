import hashlib
import json

from lib.content.lane import identity, material, selection
from lib.refusal import Refusal

READERS = {
    "name": identity.name, "digest": identity.hex_value, "address": material.address,
    "source": material.source, "build": material.build, "artifact": material.artifact,
    "capabilities": material.capabilities, "authorities": material.authorities,
    "declaration": material.declaration, "context": selection.context,
    "selection": selection.selection, "record": selection.record, "reference": selection.reference,
}


def read(kind, value):
    if not isinstance(kind, str) or kind not in READERS:
        raise Refusal("unsupported lane codec kind")
    return READERS[kind](value)


def encode(kind, value):
    return json.dumps(read(kind, value), separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(kind, value):
    return hashlib.sha256(encode(kind, value)).hexdigest()


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
