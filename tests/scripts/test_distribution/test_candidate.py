import copy
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity import bind
from lib.identity.bind import candidate, control, producer
from lib.identity.bind.spec import TARGET, entry
from lib.media import release
from lib.refusal import Refusal
from lib.store import plan, workload
from tests.scripts.test_distribution.binary_world import World


def replace(bucket, key, name, body):
    prefix = workload.prefix(key)
    record = json.loads(bucket.get(prefix + "record.json"))
    record["files"][name] = entry(body)
    bucket.objects[prefix + "blobs/" + name] = body
    bucket.objects[prefix + "record.json"] = canonical.encode(record)


class Candidate(unittest.TestCase):
    def setUp(self):
        self.reference = World()
        with self.reference.wired():
            producer.produce(self.reference.held)
        self.world = World("v1.2.3", self.reference)
        self.world.role = candidate.ROLE
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.held = dict(self.world.held, attempt="1", reference=self.reference.source["marker"], source=str(Path(self.temporary.name) / "candidate"))

    def test_unpublished_target_uses_actual_plan_and_shared_packer_without_public_writer(self):
        observed = []
        original = self.world.fetch

        def fetch(url):
            observed.append(url)
            self.assertNotIn("/v1/releases/stable/", url)
            return original(url)

        before = copy.deepcopy(self.world.documents), copy.deepcopy(self.world.public.objects)
        with self.world.wired(), mock.patch.object(release, "fetch", side_effect=fetch), mock.patch.object(candidate.r2, "writer", side_effect=AssertionError("release writer")):
            result = candidate.produce(self.held)
            writes = list(self.world.blob.writes)
            readback = candidate.consume(dict(self.held, digest=result["evidence"]["key"]))
        self.assertTrue(result["associated"])
        self.assertFalse(result["promotion"])
        self.assertEqual(readback, result)
        self.assertEqual(self.world.blob.writes, writes)
        self.assertEqual(result["expected"]["archive"]["sha256"], self.world.archive["sha256"])
        self.assertEqual(result["declaration"]["installed"], ["demo"])
        self.assertEqual(set(result["binaries"]), {"demo", "demo-api"})
        self.assertEqual(before, (self.world.documents, self.world.public.objects))
        self.assertTrue(observed)

    def test_wrong_plan_attempt_source_or_snapshot_records_refusal_not_a_pass(self):
        original = copy.deepcopy(self.world.plan)
        location = plan.location(original["context"])
        baseline = copy.deepcopy(self.world.blob.objects)
        for change in ("run", "snapshot", "declaration"):
            self.world.blob.objects = copy.deepcopy(baseline)
            document = copy.deepcopy(original)
            if change == "run":
                document["context"]["run"] = "99"
            elif change == "snapshot":
                document["context"]["snapshot"]["source"]["tree"] = "0" * 40
            else:
                document["context"]["binary_declaration"]["built"] = ["demo"]
            self.world.blob.objects[location] = canonical.encode(document)
            basis = {"schema": candidate.MATERIAL, "observer": self.world.observer, "request": {name: self.held[name] for name in candidate.REQUEST}}
            with self.subTest(change=change), self.world.wired():
                result, reference = candidate.produced(self.world.blob, basis)
                self.assertFalse(result["associated"])
                self.assertFalse(result["promotion"])
                self.assertEqual(result["state"], "refused")
                restored, _ = candidate.restore(self.world.blob, reference["key"], self.world.observer)
                self.assertEqual(restored, result)

    def test_rejected_attempt_is_durable_and_consumer_preserves_its_negative_verdict(self):
        prefix = workload.prefix(self.world.build_key)
        self.world.blob.objects[prefix + "blobs/demo-" + TARGET] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            candidate.produce(self.held)
        report = json.loads((Path(self.held["source"]) / "candidate.json").read_bytes())
        self.assertEqual(report["state"], "refused")
        self.assertFalse(report["associated"])
        with self.world.wired():
            writes = list(self.world.blob.writes)
            readback = candidate.consume(dict(self.held, digest=report["evidence"]["key"]))
        self.assertEqual(readback, report)
        self.assertEqual(writes, self.world.blob.writes)

    def test_unknown_input_is_retained_without_manufacturing_candidate_evidence(self):
        with self.world.wired(), mock.patch.object(candidate.verification, "planned", side_effect=OSError("unavailable")), self.assertRaises(Refusal):
            candidate.produce(self.held)
        report = json.loads((Path(self.held["source"]) / "candidate.json").read_bytes())
        self.assertEqual(report["state"], "unknown")
        self.assertFalse(report["associated"])
        self.assertEqual(report["reason"], "candidate evidence is unavailable or malformed")

    def test_internally_rehashed_tampering_is_rejected_by_independent_byte_verification(self):
        with self.world.wired():
            result = candidate.produce(self.held)
            key = result["evidence"]["key"]
            changed = json.loads(self.world.blob.get(workload.prefix(key) + "blobs/result.json"))
            changed["expected"]["archive"]["sha256"] = "0" * 64
            replace(self.world.blob, key, "result.json", canonical.encode(changed))
            with self.assertRaisesRegex(Refusal, "independent verification"):
                candidate.restore(self.world.blob, key, self.world.observer)

    def test_foreign_attempt_implementation_paths_and_blob_tampering_refuse(self):
        with self.world.wired():
            result = candidate.produce(self.held)
            key = result["evidence"]["key"]
            original = copy.deepcopy(self.world.blob.objects)
            for mode in ("observer", "basis", "path", "archive"):
                self.world.blob.objects = copy.deepcopy(original)
                expected = dict(self.world.observer, attempt="2") if mode == "observer" else self.world.observer
                if mode == "basis":
                    self.world.blob.objects[workload.prefix(key) + "basis.json"] = b"{}"
                elif mode == "path":
                    record = json.loads(self.world.blob.get(workload.prefix(key) + "record.json"))
                    record["files"]["../escape"] = {"size": 1, "sha256": "0" * 64}
                    self.world.blob.objects[workload.prefix(key) + "record.json"] = canonical.encode(record)
                elif mode == "archive":
                    self.world.blob.objects[workload.prefix(key) + "blobs/candidate.tar.gz"] += b"changed"
                with self.subTest(mode=mode), self.assertRaises(Refusal):
                    candidate.restore(self.world.blob, key, expected)

    def test_complete_noninstalled_file_mutation_cannot_hide_behind_the_same_key(self):
        name = "demo-api-" + TARGET
        prefix = workload.prefix(self.world.build_key)
        original = self.world.blob.get(prefix + "blobs/" + name) + b"changed"
        replace(self.world.blob, self.world.build_key, name, original)
        receipt = json.loads(self.world.blob.get(prefix + "blobs/receipt.json"))
        receipt["files"][name] = entry(original)
        replace(self.world.blob, self.world.build_key, "receipt.json", canonical.encode(receipt))
        bound = bind.bind(original, bind.inspect(self.world.bound[name])[1])
        replace(self.world.blob, self.world.bind_key, name, bound)
        receipt = json.loads(self.world.blob.get(workload.prefix(self.world.bind_key) + "blobs/receipt.json"))
        for item in receipt["binaries"]:
            if item["file"] == name:
                item.update(entry(bound))
        replace(self.world.blob, self.world.bind_key, "receipt.json", canonical.encode(receipt))
        with self.world.wired(), self.assertRaisesRegex(Refusal, "effective build"):
            candidate.produce(self.held)

    def test_no_receipt_from_a_different_source_becomes_same_source_qualification(self):
        other = World("v1.2.4-rc.1", commit="e" * 40)
        with other.wired():
            producer.produce(other.held)
        self.held["reference"] = other.source["marker"]
        with self.world.wired(), mock.patch.object(release, "fetch", side_effect=other.fetch), self.assertRaisesRegex(Refusal, "source changed"):
            candidate.produce(self.held)
        result = json.loads((Path(self.held["source"]) / "candidate.json").read_bytes())
        record = json.loads(self.world.blob.get(workload.prefix(result["evidence"]["key"]) + "record.json"))
        self.assertEqual(set(record["files"]), candidate.FILES)
        self.assertFalse(result["associated"])

    def test_untrusted_role_refuses_before_any_material_write(self):
        before = list(self.world.blob.writes)
        with self.world.wired(), mock.patch.dict(os.environ, {"GITHUB_JOB": control.ROLE}), self.assertRaises(Refusal):
            candidate.produce(self.held)
        self.assertEqual(before, self.world.blob.writes)
        self.assertFalse(Path(self.held["source"]).exists())
