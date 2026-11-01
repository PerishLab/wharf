import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind.spec import entry
from lib.process.promotion import assessment, material, operation, reader
from lib.refusal import Refusal
from lib.store import workload
from tests.promotion.world import World


class Material(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)

    def test_unpublished_stable_prepares_and_independently_repeats_without_product_writer(self):
        w = self.world
        with w.wired(), mock.patch.object(operation.r2, "writer", side_effect=AssertionError("release writer")), mock.patch.object(assessment.reader, "check", wraps=reader.check) as checked:
            prepared = operation.prepare(w.held)
            writes = list(w.world.blob.writes)
            read = operation.read(dict(w.held, digest=prepared["evidence"]["key"]))
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(read["state"], "prepared")
        self.assertFalse(read["promotion"])
        self.assertFalse(read["complete_publication"])
        self.assertIn("oci", read["unassessed"])
        self.assertGreaterEqual(checked.call_count, 4)
        self.assertEqual(w.world.blob.writes, writes)
        self.assertTrue((Path(w.held["source"]) / "readback.json").is_file())

    def test_current_qualification_and_actual_candidate_cannot_be_substituted(self):
        w = self.world
        for kind in ("archive", "manifest", "source"):
            bad = copy.deepcopy(w.passed)
            if kind == "archive":
                bad["products"][0]["archive"] = "0" * 64
            elif kind == "manifest":
                bad["declaration"]["manifest"] = "0" * 64
            else:
                bad["source"]["tree"] = "0" * 40
            ref = w.world.reference(w.reference.root + f"/windtun/{kind}/pass.json", canonical.encode(bad), "application/json")
            w.qualified["report"]["evidence"]["credential"] = ref
            with self.subTest(kind=kind), w.wired():
                expected = operation.basis(w.held)
                result, _ = operation.produce(w.world.blob, dict(expected, started=expected["started"].replace("10:00", {"archive": "10:01", "manifest": "10:02", "source": "10:03"}[kind])))
                self.assertEqual(result["state"], "refused")
                self.assertFalse(result["promotion"])

    def test_new_pending_or_unknown_is_durable_and_never_a_prepared_success(self):
        w = self.world
        for state, failure in (("refused", Refusal("newer pending")), ("unknown", OSError("unavailable"))):
            with self.subTest(state=state), w.wired(), mock.patch.object(reader, "check", side_effect=failure):
                expected = operation.basis(w.held)
                expected["started"] = expected["started"].replace("10:00", "10:01" if state == "refused" else "10:02")
                result, key = operation.produce(w.world.blob, expected)
                restored, _ = material.restore(w.world.blob, key, expected)
            self.assertEqual(restored, result)
            self.assertEqual(result["state"], state)
            self.assertFalse(result["qualification"])

    def test_successful_material_does_not_cache_a_later_qualification_failure(self):
        w = self.world
        with w.wired():
            prepared = operation.prepare(w.held)
            with mock.patch.object(reader, "check", side_effect=Refusal("newer cancelled")), self.assertRaisesRegex(Refusal, "cancelled"):
                operation.read(dict(w.held, digest=prepared["evidence"]["key"]))

    def test_rehashed_result_or_candidate_bytes_still_need_independent_verification(self):
        w = self.world
        with w.wired():
            expected = operation.basis(w.held)
            _, key = operation.produce(w.world.blob, expected)
            original = copy.deepcopy(w.world.blob.objects)
            prefix = workload.prefix(key)
            for name in ("result.json", "candidate.tar.gz"):
                w.world.blob.objects = copy.deepcopy(original)
                raw = w.world.blob.get(prefix + "blobs/" + name)
                if name == "result.json":
                    result = json.loads(raw)
                    result["plan"] = "0" * 64
                    raw = canonical.encode(result)
                else:
                    raw += b"changed"
                record = json.loads(w.world.blob.get(prefix + "record.json"))
                record["files"][name] = entry(raw)
                w.world.blob.objects[prefix + "record.json"] = canonical.encode(record)
                w.world.blob.objects[prefix + "blobs/" + name] = raw
                with self.subTest(name=name), self.assertRaises(Refusal):
                    material.restore(w.world.blob, key, expected)

    def test_attempt_path_inventory_and_foreign_implementation_cannot_be_adopted(self):
        w = self.world
        with w.wired():
            expected = operation.basis(w.held)
            _, key = operation.produce(w.world.blob, expected)
            with self.assertRaises(Refusal):
                material.restore(w.world.blob, key, dict(expected, observer=dict(expected["observer"], attempt="2")))
            prefix = workload.prefix(key)
            record = json.loads(w.world.blob.get(prefix + "record.json"))
            record["files"]["../escape"] = entry(b"changed")
            w.world.blob.objects[prefix + "record.json"] = canonical.encode(record)
            with self.assertRaises(Refusal):
                material.restore(w.world.blob, key, expected)

    def test_rehashed_negative_record_cannot_add_success_or_foreign_fields(self):
        w = self.world
        with w.wired(), mock.patch.object(reader, "check", side_effect=Refusal("pending")):
            expected = operation.basis(w.held)
            _, key = operation.produce(w.world.blob, expected)
            prefix = workload.prefix(key)
            result = json.loads(w.world.blob.get(prefix + "blobs/result.json"))
            result["foreign"] = True
            raw = canonical.encode(result)
            record = json.loads(w.world.blob.get(prefix + "record.json"))
            record["files"]["result.json"] = entry(raw)
            w.world.blob.objects[prefix + "record.json"] = canonical.encode(record)
            w.world.blob.objects[prefix + "blobs/result.json"] = raw
            with self.assertRaisesRegex(Refusal, "fields"):
                material.restore(w.world.blob, key, expected)
