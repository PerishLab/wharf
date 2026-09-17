import hashlib
import json
import re
import struct

from lib.identity.format import DIGITS, FIELDS, LAYOUT, MAGIC, MARKER, PAYLOAD, SIZE
from lib.refusal import Refusal


def text(raw):
    end = raw.find(b"\0")
    end = len(raw) if end < 0 else end
    if any(raw[end:]):
        raise Refusal("identity field contains noncanonical padding")
    return raw[:end].decode()


def field(region, name):
    start, end = LAYOUT[name]
    return region[start:end]


def hexadecimal(value, length):
    if len(value) != length or not re.fullmatch(r"[0-9a-f]+", value):
        raise Refusal(f"identity requires a lowercase {length}-digit digest")


def checksum(region):
    start, end = LAYOUT["checksum"]
    return hashlib.sha256(region[:start] + region[end:]).digest()


def verify(binding, origin):
    if not re.fullmatch(MARKER, binding["marker"]):
        raise Refusal(f"identity marker {binding['marker']!r} carries an unsupported channel")
    if not binding["product"] or binding["product"].upper().replace("-", "_") != origin["prefix"]:
        raise Refusal("identity product differs from the executable")
    for name, length in DIGITS.items():
        hexadecimal(binding[name], length)


def origin(region):
    if len(region) != SIZE or field(region, "magic") != MAGIC:
        raise Refusal("identity region has an unknown format or size")
    held = {name: text(field(region, name)) for name in ("prefix", "commit", "target")}
    if not re.fullmatch(r"[A-Z_]+", held["prefix"]):
        raise Refusal("identity region carries an invalid product prefix")
    if held["commit"]:
        hexadecimal(held["commit"], DIGITS["commit"])
    return held


def payload(region, held):
    (length,) = struct.unpack("<Q", field(region, "length"))
    if length > SIZE - PAYLOAD or any(region[PAYLOAD + length:]):
        raise Refusal("identity payload or padding exceeds its declared bounds")
    if checksum(region) != field(region, "checksum"):
        raise Refusal("identity checksum differs from its content")
    binding = json.loads(region[PAYLOAD:PAYLOAD + length])
    if set(binding) != set(FIELDS):
        raise Refusal("identity binding fields differ from the format")
    verify(binding, held)
    return binding


def decode(region):
    held = origin(region)
    if struct.unpack("<Q", field(region, "length"))[0] == 0:
        if any(region[LAYOUT["length"][1]:]):
            raise Refusal("unbound identity region contains unexpected data")
        return held, None
    return held, payload(region, held)


def encode(region, binding):
    held, bound = decode(region)
    verify(binding, held)
    if bound is not None:
        if bound != binding:
            raise Refusal("identity is already bound to another publication")
        return bytes(region)
    body = json.dumps({name: binding[name] for name in FIELDS}, separators=(",", ":")).encode()
    if len(body) > SIZE - PAYLOAD:
        raise Refusal("identity payload exceeds reserved capacity")
    result = bytearray(region)
    result[slice(*LAYOUT["length"])] = struct.pack("<Q", len(body))
    result[PAYLOAD:PAYLOAD + len(body)] = body
    result[slice(*LAYOUT["checksum"])] = checksum(result)
    decode(bytes(result))
    return bytes(result)
