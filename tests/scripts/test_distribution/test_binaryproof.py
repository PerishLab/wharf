import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind import comparison, control, producer
from lib.identity.bind.spec import issuer
from lib.refusal import Refusal
from tests.scripts.test_distribution.binary_world import AUTHORITY, World


class Proof(unittest.TestCase):
    def test_public_proofs_verify_real_bytes_and_allow_only_identity_change(self):
        candidate = World()
        with candidate.wired():
            first = producer.produce(candidate.held)
            self.assertEqual(producer.produce(candidate.held), first)
        stable = World("v1.2.3", candidate)
        with stable.wired():
            producer.produce(stable.held)
            result = comparison.compare("PerishLab/demo", "v1.2.3-rc.1", "v1.2.3")
            public, restored = comparison.read("PerishLab/demo", "v1.2.3")
        self.assertTrue(result["associated"])
        self.assertFalse(result["promotion"])
        self.assertEqual(result["installed"], ["demo"])
        self.assertEqual(set(public["binaries"]), {"demo", "demo-api"})
        self.assertEqual(set(restored), {"demo"})

    def test_failed_or_foreign_native_context_refuses_before_publication(self):
        world = World()
        with world.wired(), mock.patch.dict(os.environ, {"GITHUB_JOB": "windtun"}), self.assertRaises(Refusal):
            producer.produce(world.held)
        self.assertEqual(world.public.objects, {})

    def test_native_fork_branch_attempt_and_incomplete_job_inventory_refuse(self):
        world = World()
        for change in ({"head_branch": "feature"}, {"event": "pull_request"}, {"run_attempt": 2}, {"head_repository": {"full_name": "Else/wharf", "id": 2}}):
            def api(path):
                held = world.api(path)
                return dict(held, **change) if path.startswith("actions/runs/") and "/jobs?" not in path else held
            with self.subTest(change=change), world.wired(), mock.patch.object(control, "api", side_effect=api), self.assertRaises(Refusal):
                control.active(world.observer)
        for change in ("missing", "duplicate"):
            def api(path):
                held = world.api(path)
                if "/jobs?" in path:
                    if change == "missing":
                        held["total_count"] += 1
                    else:
                        held["jobs"] *= 2
                        held["total_count"] = 2
                return held
            with self.subTest(change=change), world.wired(), mock.patch.object(control, "api", side_effect=api), self.assertRaises(Refusal):
                control.active(world.observer)
        with world.wired():
            for change in ({"run": "9"}, {"repository": "PerishLab/else"}, {"marker": "v1.2.4"}):
                with self.subTest(change=change), self.assertRaises((Refusal, KeyError)):
                    producer.produce(dict(world.held, **change))
        self.assertEqual(world.public.objects, {})

    def test_missing_inventory_or_plan_identity_refuses(self):
        world = World()
        with world.wired():
            public = control.public(world.held)
            changed = copy.deepcopy(world.plan)
            changed["context"]["commit"] = "f" * 40
            from lib.store import plan
            world.blob.objects[plan.location(world.plan["context"])] = canonical.encode(changed)
            with self.assertRaises(Refusal):
                producer.planned(world.blob, public)
            changed = copy.deepcopy(world.plan)
            changed["context"].pop("binary_declaration")
            with self.assertRaises(Refusal):
                producer.verified(world.blob, public, changed)

    def test_publication_and_record_tamper_never_become_reuse(self):
        world = World()
        with world.wired():
            result = producer.produce(world.held)
            value = json.loads(world.fetch(result["proof"]["url"]))
            self.assertEqual(value["issuer"], issuer())
            changed = copy.deepcopy(value)
            changed["files"]["demo"]["size"] += 1
            with self.assertRaises(Refusal):
                producer.publish({"source": world.source}, changed, world.observer, world.started)
            from lib.store import workload
            name = workload.prefix(result["evidence"]["key"]) + "record.json"
            world.blob.objects[name] = b"{}"
            with self.assertRaises(Refusal):
                producer.produce(world.held)

    def test_later_retry_of_older_run_advances_discovery_without_mutating_proof(self):
        world = World()
        with world.wired():
            first = producer.produce(world.held)
            value = json.loads(world.fetch(first["proof"]["url"]))
            later = dict(world.observer, run="19", attempt="2")
            self.assertEqual(producer.publish({"source": world.source}, value, later, "2026-10-30T10:01:00Z"), first["proof"])
            producer.publish({"source": world.source}, value, world.observer, world.started)
            held = json.loads(world.public.get(world.root + "/build/linux-x64.json"))
            self.assertEqual(held["observer"], later)
            self.assertEqual(world.fetch(first["proof"]["url"]), canonical.encode(value))

    def test_public_reader_refuses_reference_or_proof_hash_tamper(self):
        world = World()
        with world.wired():
            result = producer.produce(world.held)
            name = world.root + "/build/linux-x64.json"
            held = json.loads(world.public.get(name))
            held["proof"]["url"] = "https://foreign.test/proof.json"
            world.public.objects[name] = canonical.encode(held)
            with self.assertRaises(Refusal):
                comparison.read("PerishLab/demo", world.source["marker"])
            held["proof"] = result["proof"]
            world.public.objects[name] = canonical.encode(held)
            key = result["proof"]["url"].removeprefix(AUTHORITY + "/")
            world.public.objects[key] += b"tampered"
            with self.assertRaises(Refusal):
                comparison.read("PerishLab/demo", world.source["marker"])

    def test_content_comparison_does_not_accept_same_source_or_key_alone(self):
        candidate = World()
        with candidate.wired():
            producer.produce(candidate.held)
        stable = World("v1.2.3", candidate)
        with stable.wired():
            producer.produce(stable.held)
            before, files = comparison.read("PerishLab/demo", candidate.source["marker"])
            after, other = comparison.read("PerishLab/demo", stable.source["marker"])
        for change in ({"source": dict(after["source"], tree="a" * 40)}, {"declaration": dict(after["declaration"], installed=["demo-api"])}, {"build": dict(after["build"], basis="0" * 64)}, {"resolution": "0" * 64}):
            with self.subTest(change=change), mock.patch.object(comparison, "read", side_effect=[(before, files), (dict(after, **change), other)]), self.assertRaises(Refusal):
                comparison.compare("PerishLab/demo", candidate.source["marker"], stable.source["marker"])
        with mock.patch.object(comparison, "read", side_effect=[(before, files), (after, {"demo": b"different"})]), self.assertRaises(Refusal):
            comparison.compare("PerishLab/demo", candidate.source["marker"], stable.source["marker"])
