import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind import candidate as linux_candidate, producer as linux
from lib.identity.bind.spec import entry
from lib.process.association import candidate, producer
from lib.refusal import Refusal
from lib.store import workload
from tests.association.fixtures import MACOS, WINDOWS
from tests.association.world import World


class Candidate(unittest.TestCase):
    def setUp(self):
        self.reference = World()
        self.reference.role = "build-proof"
        with self.reference.wired():
            linux.produce(self.reference.held)
        self.reference.role = "native-proof"
        with self.reference.wired():
            producer.produce(self.reference.held)
        self.world = World("v1.2.3", self.reference)
        self.world.role = "build-candidate"
        self.hold = self.enterContext(tempfile.TemporaryDirectory())
        self.held = dict(self.world.held, attempt="1", reference=self.reference.source["marker"], source=str(Path(self.hold) / "linux"))
        with self.world.wired():
            linux_candidate.produce(self.held)
        self.world.role = "native-candidate"
        self.held["source"] = str(Path(self.hold) / "native")

    def test_unpublished_plan_covers_both_targets_and_exact_linux_material(self):
        original = self.world.fetch
        calls = []

        def fetch(url):
            calls.append(url)
            self.assertNotIn("/v1/releases/stable/", url)
            self.assertFalse(url.endswith("/v1/channels/stable.json"))
            return original(url)

        with self.world.wired(), mock.patch.object(self.world, "fetch", side_effect=fetch), mock.patch.object(candidate.r2, "writer", side_effect=AssertionError("writer")):
            result = candidate.produce(self.held)
            before = list(self.world.blob.writes)
            readback = candidate.consume(dict(self.held, digest=result["evidence"]["key"]))
            self.assertEqual(before, self.world.blob.writes)
        self.assertEqual(result, readback)
        self.assertTrue(result["associated"])
        self.assertFalse(result["promotion"])
        self.assertEqual(set(result["targets"]), {MACOS, WINDOWS})
        self.assertEqual(result["managers"]["canonical"]["state"], "expected")
        self.assertTrue(calls)

    def test_rehashed_result_is_rejected_by_independent_verification(self):
        with self.world.wired():
            result = candidate.produce(self.held)
            prefix = workload.prefix(result["evidence"]["key"])
            body = json.loads(self.world.blob.get(prefix + "blobs/result.json"))
            body["targets"][MACOS]["expected"]["sha256"] = "0" * 64
            raw = canonical.encode(body)
            record = json.loads(self.world.blob.get(prefix + "record.json"))
            record["files"]["result.json"] = entry(raw)
            self.world.blob.objects[prefix + "blobs/result.json"] = raw
            self.world.blob.objects[prefix + "record.json"] = canonical.encode(record)
            with self.assertRaisesRegex(Refusal, "independent verification"):
                candidate.consume(dict(self.held, digest=result["evidence"]["key"]))

    def test_corrupt_native_build_is_durable_refusal_and_cannot_fall_back_to_linux(self):
        key = self.world.native[WINDOWS]["build"]
        prefix = workload.prefix(key)
        file = next(name for name in self.world.native[WINDOWS]["built"] if name.endswith(".exe"))
        self.world.blob.objects[prefix + "blobs/" + file] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            candidate.produce(self.held)
        report = json.loads((Path(self.held["source"]) / "candidate.json").read_bytes())
        self.assertFalse(report["associated"])
        self.assertFalse(report["promotion"])
        self.assertEqual(report["state"], "refused")
        with self.world.wired():
            result = candidate.consume(dict(self.held, digest=report["evidence"]["key"]))
        self.assertEqual(result, report)

    def test_foreign_attempt_and_unknown_material_do_not_become_associated(self):
        with self.world.wired():
            result = candidate.produce(self.held)
            with self.assertRaises(Refusal):
                candidate.restore(self.world.blob, result["evidence"]["key"], dict(self.world.observer, attempt="2"))
        with self.world.wired(), mock.patch.object(candidate, "assess", side_effect=OSError("unavailable")):
            other = dict(self.world.observer, run="22")
            result, _ = candidate.produced(self.world.blob, {"schema": candidate.MATERIAL, "observer": other, "request": {name: self.held[name] for name in linux_candidate.REQUEST}})
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["associated"])

    def test_published_stable_canonical_managers_and_content_are_verified(self):
        self.world.role = "native-proof"
        with self.world.wired():
            result = producer.produce(self.world.held)
            from lib.process.association import public
            for target in result["proofs"]:
                left, before = public.read("PerishLab/demo", self.reference.source["marker"], target)
                right, after = public.read("PerishLab/demo", self.world.source["marker"], target)
                public.associated(left, right, before, after)
                self.assertEqual(right["managers"]["canonical"]["state"], "associated")
        self.world.documents["https://releases.demo.perish.uk/manage.ps1"] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], WINDOWS)
