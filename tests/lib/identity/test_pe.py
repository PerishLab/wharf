import struct
import unittest

from lib.identity import bind, pe
from lib.refusal import Refusal
from tests.lib.identity.test_bind import BINDING, region

TARGET = "x86_64-pc-windows-msvc"


def image(content, sections=(".releaseid",), security=(0, 0), flags=0x0022):
    names = [name.encode() for name in sections]
    strings = b""
    raw_names = []
    for name in names:
        if len(name) > 8:
            raw_names.append(b"/%d" % (4 + len(strings)))
            strings += name + b"\0"
        else:
            raw_names.append(name)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<I", optional, 108, 16)
    struct.pack_into("<II", optional, 112 + 4 * 8, *security)
    table = 64 + 24 + len(optional)
    data = table + 40 * len(names)
    headers = b"".join(struct.pack("<8sIIIIIIHHI", raw, len(content), 0x1000 * (index + 1), len(content), data + index * len(content), 0, 0, 0, 0, 0) for index, raw in enumerate(raw_names))
    symbols = data + len(content) * len(names)
    coff = struct.pack("<HHIIIHH", 0x8664, len(names), 0, symbols, 0, len(optional), flags)
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 60, 64)
    table_bytes = struct.pack("<I", 4 + len(strings)) + strings
    return bytes(dos) + b"PE\0\0" + coff + bytes(optional) + headers + content * len(names) + table_bytes


class Portable(unittest.TestCase):
    def test_binds_and_reads_back(self):
        held = image(region(target=TARGET))
        bound = bind.bind(held, BINDING)
        self.assertEqual(len(bound), len(held))
        self.assertEqual(bind.inspect(bound), ({"prefix": "PLUMB", "commit": "", "target": TARGET}, BINDING))

    def test_resolves_long_names_through_the_string_table(self):
        held = image(region(target=TARGET), sections=(".text", ".releaseid"))
        start, end = pe.locate(held)
        self.assertEqual(held[start:end], region(target=TARGET))

    def test_refuses_an_authenticode_signed_input(self):
        with self.assertRaisesRegex(Refusal, "Authenticode"):
            bind.inspect(image(region(target=TARGET), security=(0x4000, 0x200)))

    def test_refuses_an_object_file(self):
        with self.assertRaisesRegex(Refusal, "linked executable"):
            bind.inspect(image(region(target=TARGET), flags=0))

    def test_names_the_sections_it_found_when_the_region_is_missing(self):
        with self.assertRaisesRegex(Refusal, r"\.release'"):
            bind.inspect(image(region(target=TARGET), sections=(".release",)))

    def test_refuses_duplicate_or_short_regions(self):
        for held in (image(region(target=TARGET), sections=(".releaseid", ".releaseid")), image(region(target=TARGET)[:512])):
            with self.subTest(), self.assertRaises(Refusal):
                bind.inspect(held)
