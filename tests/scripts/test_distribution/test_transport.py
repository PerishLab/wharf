import json
import tempfile
from pathlib import Path
import unittest

from lib.content import canonical
from lib.process.trial import context, transport
from lib.refusal import Refusal
from lib.store import workload
from tests.scripts.test_distribution.world import World


class Material(unittest.TestCase):
    def test_failed_and_incomplete_bytes_survive_and_record_is_last(self):
        world = World()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = world.write(root / "evidence")
            (evidence / "report.json").write_bytes(b'{"state":"failed"')
            material = transport.publish(world.blob, world.prepared, "execution", evidence)
            self.assertEqual(world.blob.writes[-1], workload.prefix(material["key"]) + "record.json")
            transport.retrieve(world.blob, world.prepared, "execution", root / "readback")
            self.assertEqual((root / "readback/report.json").read_bytes(), b'{"state":"failed"')
            self.assertEqual((root / "readback/run.log").read_text(), "original assertion output\n")
            self.assertEqual(transport.publish(world.blob, world.prepared, "execution", evidence), material)
            (evidence / "run.log").write_text("different attempt content")
            with self.assertRaises(Refusal):
                transport.publish(world.blob, world.prepared, "execution", evidence)

    def test_retry_changes_address_and_record_context_cannot_be_silently_reused(self):
        world = World()
        prepared = world.prepared
        changed = dict(prepared, observer=dict(prepared["observer"], attempt="2"))
        self.assertNotEqual(context.address(prepared, "execution"), context.address(changed, "execution"))
        with tempfile.TemporaryDirectory() as directory:
            evidence = world.write(directory)
            material = transport.publish(world.blob, prepared, "execution", evidence)
            name = workload.prefix(material["key"]) + "record.json"
            record = json.loads(world.blob.get(name))
            world.blob.objects[name] = canonical.encode(dict(record, context={}))
            with self.assertRaises(Refusal):
                transport.publish(world.blob, prepared, "execution", evidence)

    def test_tampered_bytes_and_record_paths_refuse_before_restore(self):
        world = World()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = transport.publish(world.blob, world.prepared, "execution", world.write(root / "evidence"))
            prefix = workload.prefix(material["key"])
            world.blob.objects[prefix + "blobs/run.log"] = b"tampered"
            with self.assertRaises(Refusal):
                transport.retrieve(world.blob, world.prepared, "execution", root / "restore")
            record = json.loads(world.blob.get(prefix + "record.json"))
            record["files"]["../../escaped"] = {"size": 1, "sha256": "a" * 64}
            world.blob.objects[prefix + "record.json"] = canonical.encode(record)
            with self.assertRaises(Refusal):
                transport.retrieve(world.blob, world.prepared, "execution", root / "safe")
            self.assertFalse((root / "safe").exists())

    def test_symlinks_and_oversized_evidence_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, destination = root / "evidence", root / "snapshot"
            evidence.mkdir()
            destination.mkdir()
            (evidence / "run.log").symlink_to("/etc/passwd")
            with self.assertRaises(Refusal):
                transport.capture(evidence, destination)
            (evidence / "run.log").unlink()
            with (evidence / "run.log").open("wb") as stream:
                stream.truncate(transport.LIMIT + 1)
            with self.assertRaises(Refusal):
                transport.capture(evidence, destination)
