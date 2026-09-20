import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

from lib.identity import bind, macho, signature
from lib.refusal import Refusal
from tests.lib.identity.test_bind import BINDING, region

TARGET = "aarch64-apple-darwin"


def image(content, sections=(("__DATA", "__relid"),), filetype=2, flags=0):
    commands = 72 + 80 * len(sections)
    start = 32 + commands
    entries = b"".join(struct.pack("<16s16sQQIIIII12x", name.encode(), segment.encode(), 0, len(content), start + index * len(content), 0, 0, 0, flags) for index, (segment, name) in enumerate(sections))
    command = struct.pack("<II16sQQQQIIII", macho.SEGMENT, commands, b"__DATA", 0, 0, 0, 0, 3, 3, len(sections), 0) + entries
    header = macho.MAGIC + struct.pack("<iiIIIII", 0x0100000C, 0, filetype, 1, commands, 0, 0)
    return header + command + content * len(sections)


class MachO(unittest.TestCase):
    def test_binds_and_reads_back(self):
        held = image(region(target=TARGET))
        bound = bind.bind(held, BINDING)
        self.assertEqual(len(bound), len(held))
        self.assertEqual(bind.inspect(bound), ({"prefix": "PLUMB", "commit": "", "target": TARGET}, BINDING))

    def test_names_the_sections_it_found_when_the_region_is_missing(self):
        with self.assertRaisesRegex(Refusal, "__TEXT,__text"):
            bind.inspect(image(region(target=TARGET), sections=(("__TEXT", "__text"),)))

    def test_refuses_a_region_outside_its_data_segment(self):
        with self.assertRaisesRegex(Refusal, "reserved data segment"):
            bind.inspect(image(region(target=TARGET), sections=(("__TEXT", "__relid"),)))

    def test_refuses_zero_fill_duplicate_or_non_executable_images(self):
        for held in (
            image(region(target=TARGET), flags=0x1),
            image(region(target=TARGET), sections=(("__DATA", "__relid"),) * 2),
            image(region(target=TARGET), filetype=6),
            b"\xca\xfe\xba\xbe" + bytes(100),
        ):
            with self.subTest(), self.assertRaises(Refusal):
                bind.inspect(held)


class Finalize(unittest.TestCase):
    def test_leaves_other_targets_untouched(self):
        self.assertIsNone(signature.finalize(Path("/x/demo"), "x86_64-unknown-linux-gnu", runner=None))

    def test_signs_ad_hoc_beside_the_executable(self):
        calls = []
        self.assertEqual(signature.finalize(Path("/x/demo"), TARGET, lambda argv, cwd: calls.append((argv, cwd))), "adhoc")
        self.assertEqual(calls, [(["rcodesign", "sign", "/x/demo"], Path("/x"))])

    def test_the_signer_is_the_same_one_on_every_platform(self):
        for platform in ("linux", "darwin", "win32"):
            calls = []
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                signature.finalize(Path("/x/demo"), TARGET, lambda argv, cwd: calls.append(argv))
            self.assertEqual(calls, [["rcodesign", "sign", "/x/demo"]])
