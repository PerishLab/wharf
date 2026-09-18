import struct

from lib.identity.format import SECTIONS, SIZE
from lib.refusal import Refusal

MAGIC = b"\xcf\xfa\xed\xfe"
EXECUTE = 2
SEGMENT = 0x19
ZEROFILL = (0x1, 0xC, 0x12)


def text(raw):
    return raw.rstrip(b"\0").decode(errors="replace")


def commands(image):
    if image[:4] != MAGIC:
        raise Refusal("identity binding requires a thin 64-bit little-endian Mach-O")
    filetype, count, _ = struct.unpack_from("<III", image, 12)
    if filetype != EXECUTE:
        raise Refusal("identity binding requires a linked executable")
    cursor = 32
    for _ in range(count):
        command, size = struct.unpack_from("<II", image, cursor)
        yield command, cursor
        cursor += size


def sections(image):
    listed = []
    for command, cursor in commands(image):
        if command != SEGMENT:
            continue
        (count,) = struct.unpack_from("<I", image, cursor + 64)
        for index in range(count):
            entry = struct.unpack_from("<16s16sQQIIIII", image, cursor + 72 + index * 80)
            listed.append(((text(entry[1]), text(entry[0])), entry[3:]))
    return listed


def locate(image):
    listed = sections(image)
    expected = SECTIONS["macho"]
    found = [fields for (segment, name), fields in listed if name == expected["section"]]
    if not found:
        names = sorted({f"{segment},{name}" for (segment, name), _ in listed})
        raise Refusal(f"executable has no release identity region; Mach-O sections are {names}")
    if len(found) > 1:
        raise Refusal("executable has multiple identity regions")
    if not any(segment == expected["segment"] and name == expected["section"] for (segment, name), _ in listed):
        raise Refusal("Mach-O identity must reside in its reserved data segment")
    size, start, _, _, relocations, flags = found[0]
    if flags & 0xFF in ZEROFILL or not start:
        raise Refusal("identity region has no file content")
    if size != SIZE or start + size > len(image):
        raise Refusal("identity region has invalid bounds")
    if relocations:
        raise Refusal("identity region must not carry relocations")
    return start, start + size
