import copy
import struct
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity import bind
from lib.process.association import binary, chain, signature
from lib.process.association.spec import filename
from lib.refusal import Refusal
from tests.association.fixtures import MACOS, WINDOWS, executable, sign
from tests.association.world import World

SOURCE = {"repository": "PerishLab/demo", "marker": "v1.2.3-rc.1", "commit": "b" * 40, "tree": "c" * 40}
BINDING = {"product": "demo", "marker": SOURCE["marker"], "commit": SOURCE["commit"], "digest": canonical.digest(SOURCE), "workload": "d" * 64}


class Binary(unittest.TestCase):
    def test_complete_normalization_roundtrip_for_both_targets(self):
        for selected in (MACOS, WINDOWS):
            body = executable("demo", selected)
            with self.subTest(target=selected), mock.patch.object(signature, "sign", side_effect=sign):
                bound = binary.transformed(body, BINDING, "demo")
                normalized, facts = binary.inspect(bound, (selected, SOURCE, BINDING["workload"]), "demo")
                self.assertEqual(normalized, binary.normalized(body, "demo", selected))
                self.assertEqual(facts["origin"]["target"], selected)
                self.assertEqual(binary.transformed(normalized, BINDING, "demo"), bound)

    def test_bad_signature_or_changed_code_cannot_pass_idempotence(self):
        body = sign(bind.bind(executable("demo", MACOS), BINDING), filename("demo", MACOS))
        for offset in (len(body) - 1, 40):
            changed = bytearray(body)
            changed[offset] ^= 1
            with self.subTest(offset=offset), mock.patch.object(signature, "sign", side_effect=sign), self.assertRaises(Refusal):
                binary.inspect(bytes(changed), (MACOS, SOURCE, BINDING["workload"]), "demo")

    def test_signed_windows_missing_reproducibility_and_wrong_machine_refuse(self):
        body = bytearray(bind.bind(executable("demo", WINDOWS), BINDING))
        for offset, value in ((88 + 112 + 4 * 8, 1), (64 + 4, 0x14c), (88 + 112 + 6 * 8, 0)):
            changed = bytearray(body)
            struct.pack_into("<I" if offset != 68 else "<H", changed, offset, value)
            with self.subTest(offset=offset), self.assertRaises(Refusal):
                binary.inspect(bytes(changed), (WINDOWS, SOURCE, BINDING["workload"]), "demo")

    def test_foreign_binding_and_truncated_images_refuse(self):
        body = bind.bind(executable("demo", WINDOWS), BINDING)
        with self.assertRaises(Refusal):
            binary.inspect(body, (WINDOWS, dict(SOURCE, commit="e" * 40), BINDING["workload"]), "demo")
        for body in (b"MZ", bytes(64), body[:100]):
            with self.subTest(size=len(body)), self.assertRaises(Refusal):
                binary.inspect(body, (WINDOWS, SOURCE, BINDING["workload"]), "demo")

    def test_noninstalled_binary_and_complete_receipts_are_verified(self):
        world = World()
        with world.wired():
            facts = chain.verify(world.blob, world.source, world.plan, (world.model, MACOS))
            self.assertEqual(set(facts["binaries"]), {"demo", "demo-api"})
            loaded = copy.deepcopy(facts["binding"])
            loaded["files"][filename("demo-api", MACOS)] += b"changed"
            with self.assertRaises(Refusal):
                chain.binaries((world.source, MACOS), world.model["targets"][MACOS], facts["build"], loaded)
            loaded = copy.deepcopy(facts["binding"])
            loaded["receipt"]["binaries"].pop()
            with self.assertRaises(Refusal):
                chain.binaries((world.source, MACOS), world.model["targets"][MACOS], facts["build"], loaded)
