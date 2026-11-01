import hashlib
import struct

from tests.lib.identity.test_bind import region
from tests.lib.identity.test_macho import image as macho
from tests.lib.identity.test_pe import image as portable

MACOS = "aarch64-apple-darwin"
WINDOWS = "x86_64-pc-windows-msvc"


def sign(body, name):
    return body[:-32] + hashlib.sha256(body[:-32] + name.encode()).digest()


def executable(name, target):
    if target == MACOS:
        return macho(region("DEMO", MACOS)) + name.encode() + bytes(32)
    body = bytearray(portable(region("DEMO", WINDOWS), sections=(".relid", ".rdata")))
    struct.pack_into("<II", body, 88 + 112 + 6 * 8, 0x2000, 28)
    start = 64 + 24 + 240 + 80 + 4096
    body[start:start + 28] = struct.pack("<IIHHIIII", 0, 0, 0, 0, 16, 0, 0, 0)
    body[start + 32:start + 32 + len(name)] = name.encode()
    return bytes(body)
