import copy
import json
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind import producer as linux
from lib.process.association import producer, public, spec
from lib.refusal import Refusal
from tests.association.fixtures import MACOS, WINDOWS
from tests.association.world import AUTHORITY, World


class Public(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def publish(self):
        with self.world.wired():
            return producer.produce(self.world.held)

    def test_real_native_plan_and_complete_records_publish_both_targets(self):
        before = copy.deepcopy(self.world.documents)
        result = self.publish()
        self.assertEqual(set(result["proofs"]), {MACOS, WINDOWS})
        self.assertFalse(result["promotion"])
        with self.world.wired():
            for selected in (MACOS, WINDOWS):
                proof, normalized = public.read("PerishLab/demo", self.world.source["marker"], selected)
                self.assertEqual(set(normalized), {"demo"})
                self.assertEqual(set(proof["binaries"]), {"demo", "demo-api"})
                self.assertEqual(proof["managers"]["canonical"]["state"], "not-published")
        self.assertEqual(before, self.world.documents)
        self.assertFalse(any(key.endswith(("seal.json", "distribution.json")) for key in self.world.public.objects))

    def test_published_and_installed_manager_checks_are_independent(self):
        self.publish()
        location = self.world.seal["managers"]["windows"]["url"]
        self.world.documents[location] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], WINDOWS)

    def test_duplicate_unknown_or_downgraded_proof_fields_refuse(self):
        result = self.publish()
        key = result["proofs"][MACOS]["url"].removeprefix(AUTHORITY + "/")
        original = self.world.public.get(key)
        for raw in (b'{"schema":1,"schema":2}', canonical.encode(dict(json.loads(original), unknown=True)), canonical.encode(dict(json.loads(original), schema="wharf.binary.association/v1"))):
            with self.subTest(raw=raw[:40]), self.world.wired(), mock.patch.object(public, "reference", return_value=raw), self.assertRaises(Refusal):
                public.read("PerishLab/demo", self.world.source["marker"], MACOS)

    def test_current_discovery_and_foreign_objects_do_not_fall_back(self):
        result = self.publish()
        location = self.world.root + "/build/native-darwin-arm64.json"
        original = self.world.public.get(location)
        index = json.loads(original)
        index["proof"]["url"] = "https://foreign.example/" + index["proof"]["name"]
        self.world.public.objects[location] = canonical.encode(index)
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], MACOS)
        self.world.public.objects[location] = original
        self.world.distribution["resolution"]["binaries"]["tree"] = "f" * 40
        self.world.documents[AUTHORITY + "/" + self.world.root + "/distribution.json"] = canonical.encode(self.world.distribution)
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], MACOS)

    def test_wrong_role_refuses_before_workload_or_release_write(self):
        self.world.role = "build-proof"
        before = copy.deepcopy(self.world.blob.objects)
        with self.world.wired(), self.assertRaises(Refusal):
            producer.produce(self.world.held)
        self.assertEqual(before, self.world.blob.objects)
        self.assertFalse(self.world.public.objects)

    def test_original_linux_proof_reader_stays_unchanged(self):
        self.world.role = "build-proof"
        with self.world.wired():
            result = linux.produce(self.world.held)
            self.assertEqual(result["state"], "proven")
        self.assertFalse(any("native-" in key for key in self.world.public.objects))
