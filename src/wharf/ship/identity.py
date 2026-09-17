"""Atomic action: bind a release identity into a built executable.

Writes the release identity region defined in spec/identity/README.md. The region
is a 4096-byte section (.releaseid in ELF) laid out as:

  0..16    magic                  80..120   origin commit
  16..80   origin prefix          120..248  origin target
  248..256 payload length (u64 LE) 256..288 sha256 over the region without 256..288
  288..    JSON binding {product, marker, digest, commit, workload}, zero padded

Only ELF executables are located for now; other formats refuse.
"""

import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

from wharf.refusal import Refusal

SIZE = 4096
MAGIC = b"RELEASE.IDENT.V2"
SECTION = ".releaseid"
PAYLOAD = 288
MARKER = re.compile(r"^v\d+\.\d+\.\d+(-(alpha|beta|rc)\.[1-9]\d*)?$")
FIELDS = ("product", "marker", "digest", "commit", "workload")


def _text(raw):
    end = raw.find(b"\0")
    end = len(raw) if end < 0 else end
    if any(raw[end:]):
        raise Refusal("identity field contains noncanonical padding")
    return raw[:end].decode()


def _hex(value, length):
    if len(value) != length or not re.fullmatch(r"[0-9a-f]+", value):
        raise Refusal(f"identity requires a lowercase {length}-digit digest")


def checksum(region):
    return hashlib.sha256(region[:256] + region[288:]).digest()


def verify(binding, origin):
    if not MARKER.match(binding["marker"]):
        raise Refusal(f"identity marker {binding['marker']!r} carries an unsupported channel")
    if not binding["product"] or binding["product"].upper().replace("-", "_") != origin["prefix"]:
        raise Refusal("identity product differs from the executable")
    _hex(binding["digest"], 64)
    _hex(binding["commit"], 40)
    _hex(binding["workload"], 64)


def decode(region):
    if len(region) != SIZE or region[:16] != MAGIC:
        raise Refusal("identity region has an unknown format or size")
    origin = {"prefix": _text(region[16:80]), "commit": _text(region[80:120]), "target": _text(region[120:248])}
    if not re.fullmatch(r"[A-Z_]+", origin["prefix"]):
        raise Refusal("identity region carries an invalid product prefix")
    if origin["commit"]:
        _hex(origin["commit"], 40)
    (length,) = struct.unpack("<Q", region[248:256])
    if length == 0:
        if any(region[256:]):
            raise Refusal("unbound identity region contains unexpected data")
        return origin, None
    if length > SIZE - PAYLOAD or any(region[PAYLOAD + length:]):
        raise Refusal("identity payload or padding exceeds its declared bounds")
    if checksum(region) != region[256:288]:
        raise Refusal("identity checksum differs from its content")
    binding = json.loads(region[PAYLOAD:PAYLOAD + length])
    if set(binding) != set(FIELDS):
        raise Refusal("identity binding fields differ from the protocol")
    verify(binding, origin)
    return origin, binding


def encode(region, binding):
    origin, held = decode(region)
    verify(binding, origin)
    if held is not None:
        if held != binding:
            raise Refusal("identity is already bound to another publication")
        return bytes(region)
    payload = json.dumps({field: binding[field] for field in FIELDS}, separators=(",", ":")).encode()
    if len(payload) > SIZE - PAYLOAD:
        raise Refusal("identity payload exceeds reserved capacity")
    result = bytearray(region)
    result[248:256] = struct.pack("<Q", len(payload))
    result[PAYLOAD:PAYLOAD + len(payload)] = payload
    result[256:288] = checksum(result)
    decode(bytes(result))
    return bytes(result)


def locate(image):
    if image[:4] != b"\x7fELF":
        raise Refusal("identity binding locates ELF executables only")
    if image[4] != 2 or image[5] != 1:
        raise Refusal("identity binding requires a 64-bit little-endian ELF")
    kind, = struct.unpack_from("<H", image, 16)
    if kind not in (2, 3):
        raise Refusal("identity binding requires a linked executable")
    shoff, = struct.unpack_from("<Q", image, 40)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", image, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", image, shoff + index * shentsize) for index in range(shnum)]
    names = sections[shstrndx]
    found = None
    for index, (name, kind, _, _, offset, size, _, info, _, _) in enumerate(sections):
        start = names[4] + name
        title = image[start:image.index(b"\0", start)].decode()
        if title != SECTION:
            continue
        if found is not None:
            raise Refusal("executable has multiple identity regions")
        if size != SIZE or offset + size > len(image):
            raise Refusal("identity region has invalid bounds")
        if any(other[1] in (4, 9) and other[7] == index for other in sections):
            raise Refusal("identity region must not carry relocations")
        found = (offset, offset + size)
    if found is None:
        raise Refusal("executable has no release identity region")
    return found


def inspect(image):
    start, end = locate(image)
    return decode(image[start:end])


def bind(image, binding):
    start, end = locate(image)
    result = image[:start] + encode(image[start:end], binding) + image[end:]
    if inspect(result)[1] != binding:
        raise Refusal("bound executable identity did not read back")
    return result


def digest(repository, marker, commit, tree):
    body = json.dumps({"commit": commit, "marker": marker, "repository": repository, "tree": tree}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def perform(directory, name, triple, repository, marker, commit, tree, workload, output):
    directory = Path(directory)
    output = Path(output)
    suffix = ".exe" if "windows" in triple else ""
    artifact = directory / f"{name}-{triple}{suffix}"
    if not artifact.is_file():
        raise Refusal(f"{artifact} is missing")
    if output.exists():
        raise Refusal(f"output {output} already exists")
    binding = {
        "product": name,
        "marker": marker,
        "digest": digest(repository, marker, commit, tree),
        "commit": commit,
        "workload": workload,
    }
    bound = bind(artifact.read_bytes(), binding)
    origin, _ = inspect(bound)
    if origin["target"] != triple:
        raise Refusal(f"executable was built for {origin['target']!r}, not {triple}")
    output.mkdir(parents=True)
    target = output / artifact.name
    target.write_bytes(bound)
    shutil.copymode(artifact, target)
    receipt = {"action": "ship.identity", "file": target.name, "binding": binding, "origin": origin,
               "sha256": hashlib.sha256(bound).hexdigest(), "size": len(bound)}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
