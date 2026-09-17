import struct

from lib.identity.format import SECTION, SIZE
from lib.refusal import Refusal

HEADER = "<IIQQQQIIQQ"
RELOCATIONS = (4, 9)


def sections(image):
    if image[:4] != b"\x7fELF":
        raise Refusal("identity binding locates ELF executables only")
    if image[4] != 2 or image[5] != 1:
        raise Refusal("identity binding requires a 64-bit little-endian ELF")
    if struct.unpack_from("<H", image, 16)[0] not in (2, 3):
        raise Refusal("identity binding requires a linked executable")
    (offset,) = struct.unpack_from("<Q", image, 40)
    width, count, strings = struct.unpack_from("<HHH", image, 58)
    headers = [struct.unpack_from(HEADER, image, offset + index * width) for index in range(count)]
    table = headers[strings][4]
    return [(image[table + header[0]:image.index(b"\0", table + header[0])].decode(), header) for header in headers]


def locate(image):
    listed = sections(image)
    found = [(index, header) for index, (name, header) in enumerate(listed) if name == SECTION]
    if not found:
        raise Refusal("executable has no release identity region")
    if len(found) > 1:
        raise Refusal("executable has multiple identity regions")
    index, header = found[0]
    start, size = header[4], header[5]
    if size != SIZE or start + size > len(image):
        raise Refusal("identity region has invalid bounds")
    if any(other[1] in RELOCATIONS and other[7] == index for _, other in listed):
        raise Refusal("identity region must not carry relocations")
    return start, start + size
