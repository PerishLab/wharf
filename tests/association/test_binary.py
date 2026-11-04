import copy
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.content import canonical
from lib.identity import bind
from lib.process.association import binary, chain, signature
from lib.process.association.spec import filename
from lib.refusal import Refusal
from tests.association.fixtures import MACOS, WINDOWS, executable, sign
from tests.association.world import World
from tests.association import world as fixture

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


class Receipts(unittest.TestCase):
    def world(self):
        manifest = fixture.MANIFEST.replace(b'binaries = ["demo", "demo-api"]', b'binaries = ["demo-api", "demo"]')
        with mock.patch.object(fixture, "MANIFEST", manifest), mock.patch.object(World, "names", ("demo-api", "demo")):
            return World()

    def finalized(self, path, target):
        if target == MACOS:
            path.write_bytes(sign(path.read_bytes(), path.name))
            return "adhoc"
        return None

    def test_actual_binding_producer_receipts_verify_on_both_native_targets(self):
        for selected in (MACOS, WINDOWS):
            world = self.world()
            with self.subTest(target=selected), world.wired():
                facts = chain.verify(world.blob, world.source, world.plan, (world.model, selected))
                self.assertEqual(facts["declaration"]["targets"][selected]["built"], ["demo-api", "demo"])
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    built = root / "built"
                    built.mkdir()
                    for name, body in facts["build"]["files"].items():
                        (built / name).write_bytes(body)
                    with mock.patch.object(bind.signature, "finalize", side_effect=self.finalized):
                        receipt = bind.perform(SimpleNamespace(directory=built, target=selected, name="demo"), bind.Release(**world.source), world.native[selected]["build"], root / "bound")
                rows = receipt["binaries"]
                self.assertEqual(rows, facts["binding"]["receipt"]["binaries"])
                self.assertEqual([row["file"] for row in rows], [filename(name, selected) for name in ("demo", "demo-api")])
                if selected == WINDOWS:
                    self.assertNotEqual([row["file"] for row in rows], sorted(row["file"] for row in rows))

    def test_wrong_order_incomplete_or_changed_receipts_remain_refused(self):
        for selected in (MACOS, WINDOWS):
            world = self.world()
            with world.wired():
                facts = chain.verify(world.blob, world.source, world.plan, (world.model, selected))
                for change in ("order", "missing", "duplicate", "origin", "signature", "bytes"):
                    bound = copy.deepcopy(facts["binding"])
                    rows = bound["receipt"]["binaries"]
                    if change == "order":
                        rows.reverse()
                    elif change == "missing":
                        rows.pop()
                    elif change == "duplicate":
                        rows[1] = copy.deepcopy(rows[0])
                    elif change == "origin":
                        rows[0]["origin"]["prefix"] = "FOREIGN"
                    elif change == "signature":
                        rows[0]["signature"] = "unsupported"
                    else:
                        bound["files"][filename("demo-api", selected)] += b"changed"
                    with self.subTest(target=selected, change=change), self.assertRaises(Refusal):
                        chain.binaries((world.source, selected), world.model["targets"][selected], facts["build"], bound)
