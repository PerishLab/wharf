import struct

from lib.identity.format import SIZE, SECTIONS
from lib.refusal import Refusal

EXECUTABLE = 0x0002
OPTIONAL = {0x10B: 96, 0x20B: 112}
SECURITY = 4
SYMBOL = 18


def header(image):
    if image[:2] != b"MZ" or len(image) < 64:
        raise Refusal("executable is not a PE image")
    (offset,) = struct.unpack_from("<I", image, 60)
    if image[offset:offset + 4] != b"PE\0\0":
        raise Refusal("executable is not a PE image")
    count, symbols, entries, width, flags = struct.unpack_from("<2xH4xIIHH", image, offset + 4)
    if not flags & EXECUTABLE:
        raise Refusal("identity binding requires a linked executable")
    optional = offset + 24
    return optional, width, count, symbols + entries * SYMBOL


def signed(image, optional):
    (magic,) = struct.unpack_from("<H", image, optional)
    if magic not in OPTIONAL:
        raise Refusal(f"PE optional header magic {magic:#x} is unsupported")
    directories = OPTIONAL[magic]
    (listed,) = struct.unpack_from("<I", image, optional + directories - 4)
    if listed <= SECURITY:
        return False
    return any(struct.unpack_from("<II", image, optional + directories + SECURITY * 8))


def name(image, raw, strings):
    raw = raw.rstrip(b"\0")
    if raw.startswith(b"/") and raw[1:].isdigit():
        start = strings + int(raw[1:])
        raw = image[start:image.index(b"\0", start)]
    return raw.decode(errors="replace")


def sections(image):
    optional, width, count, strings = header(image)
    if signed(image, optional):
        raise Refusal("identity binding refuses an Authenticode-signed input")
    table = optional + width
    listed = []
    for index in range(count):
        entry = struct.unpack_from("<8sIIII8xH", image, table + index * 40)
        listed.append((name(image, entry[0], strings), entry[1:]))
    return listed


def locate(image):
    listed = sections(image)
    found = [fields for label, fields in listed if label == SECTIONS["pe"]]
    if not found:
        names = sorted({label for label, _ in listed})
        raise Refusal(f"executable has no release identity region; PE sections are {names}")
    if len(found) > 1:
        raise Refusal("executable has multiple identity regions")
    virtual, _, raw, start, relocations = found[0]
    size = min(virtual, raw)
    if size != SIZE or start + size > len(image):
        raise Refusal("identity region has invalid bounds")
    if relocations:
        raise Refusal("identity region must not carry relocations")
    return start, start + size


def reproducible(image):
    try:
        optional, width, _, _ = header(image)
        (magic,) = struct.unpack_from("<H", image, optional)
        directories = OPTIONAL.get(magic)
        if directories is None or width < directories + 7 * 8:
            raise Refusal("Windows build has no supported PE debug directory")
        (count,) = struct.unpack_from("<I", image, optional + directories - 4)
        if count <= 6:
            raise Refusal("Windows build has no PE debug directory")
        address, size = struct.unpack_from("<II", image, optional + directories + 6 * 8)
        fields = [fields for _, fields in sections(image) if fields[1] <= address and address + size <= fields[1] + min(fields[0], fields[2])]
        if not address or not size or size % 28 or len(fields) != 1:
            raise Refusal("Windows build has invalid PE debug directory bounds")
        start = fields[0][3] + address - fields[0][1]
        if start + size > len(image):
            raise Refusal("Windows build has truncated PE debug data")
        kinds = [struct.unpack_from("<I", image, start + offset + 12)[0] for offset in range(0, size, 28)]
        if 16 not in kinds:
            raise Refusal("Windows build has no reproducible PE debug marker; the effective linker must honor /Brepro")
    except struct.error as failure:
        raise Refusal("Windows build has malformed PE headers or debug data") from failure
