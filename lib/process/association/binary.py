import struct

from lib.content import canonical
from lib.identity import bind, image, pe
from lib.identity.format import LAYOUT, SIZE
from lib.process.association import signature
from lib.process.association.spec import LIMIT, entry, filename, require, target
from lib.refusal import Refusal


def architecture(body, selected):
    target(selected)
    require(isinstance(body, bytes) and 64 <= len(body) <= LIMIT, "native executable is outside bounds")
    if selected == "aarch64-apple-darwin":
        require(body[:4] == b"\xcf\xfa\xed\xfe" and struct.unpack_from("<II", body, 4) == (0x100000c, 0), "native association needs thin arm64 Mach-O")
    else:
        require(body[:2] == b"MZ", "native association needs x86-64 PE")
        offset = struct.unpack_from("<I", body, 60)[0]
        require(body[offset:offset + 4] == b"PE\0\0" and struct.unpack_from("<H", body, offset + 4)[0] == 0x8664, "native association PE machine differs")
        pe.reproducible(body)


def neutral(body, selected):
    architecture(body, selected)
    start, end = image.locate(body)
    offset = LAYOUT["length"][0]
    result = body[:start] + body[start:start + offset] + bytes(SIZE - offset) + body[end:]
    require(bind.inspect(result)[1] is None, "native identity cannot be restored to unbound")
    return result


def normalized(body, name, selected):
    result = neutral(body, selected)
    return signature.sign(result, filename(name, selected)) if target(selected)["signature"] else result


def transformed(body, binding, name):
    selected = bind.inspect(body)[0]["target"]
    architecture(body, selected)
    result = bind.bind(body, binding)
    return signature.sign(result, filename(name, selected)) if target(selected)["signature"] else result


def observe(body, subject, name):
    selected, source, workload = subject
    architecture(body, selected)
    origin, binding = bind.inspect(body)
    expected = {"product": source["repository"].split("/")[1], "marker": source["marker"], "digest": canonical.digest(source), "commit": source["commit"], "workload": workload}
    require(binding == expected and origin == {"prefix": expected["product"].upper().replace("-", "_"), "commit": "", "target": selected}, "native executable binding or origin differs")
    if target(selected)["signature"]:
        require(signature.sign(body, filename(name, selected)) == body, "native signature is not the exact pinned transformation")
    canonical_body = normalized(body, name, selected)
    require(transformed(canonical_body, expected, name) == body, "native executable cannot reproduce its complete binding and signature")
    return canonical_body, {"origin": origin, "bound": entry(body), "normalized": entry(canonical_body), "signature": target(selected)["signature"]}


def inspect(body, subject, name):
    try:
        return observe(body, subject, name)
    except (struct.error, ValueError, IndexError, KeyError, TypeError) as failure:
        raise Refusal("native executable structure is malformed") from failure
