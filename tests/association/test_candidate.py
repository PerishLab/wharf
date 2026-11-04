import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind import candidate as linux_candidate, producer as linux
from lib.identity.bind import control
from lib.identity.bind.spec import entry
from lib.process.association import candidate, producer, public, spec
from lib.process.association import control as native
from lib.refusal import Refusal
from lib.store import plan, workload
from tests.association.fixtures import MACOS, WINDOWS
from tests.association.world import AUTHORITY, World
from tests.association.test_run import IDENTITY, REPOSITORY, run


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
        for location in list(self.world.documents):
            if location.startswith("https://raw.githubusercontent.com/"):
                del self.world.documents[location]

        def fetch(url):
            calls.append(url)
            self.assertNotIn("raw.githubusercontent.com", url)
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
            for target in result["proofs"]:
                left, before = public.read("PerishLab/demo", self.reference.source["marker"], target)
                right, after = public.read("PerishLab/demo", self.world.source["marker"], target)
                public.associated(left, right, before, after)
                self.assertEqual(right["managers"]["canonical"]["state"], "associated")
        self.world.documents["https://releases.demo.perish.uk/manage.ps1"] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], WINDOWS)


    def test_transport_failure_records_unknown_instead_of_content_refusal(self):
        failure = spec.Unavailable("tls", {"stage": "archive", "target": WINDOWS, "marker": self.reference.source["marker"]})
        with self.world.wired(), mock.patch.object(candidate, "assess", side_effect=failure):
            result, reference = candidate.produced(self.world.blob, {"schema": candidate.MATERIAL, "observer": self.world.observer, "request": {name: self.held[name] for name in linux_candidate.REQUEST}})
            readback, _ = candidate.restore(self.world.blob, reference["key"], self.world.observer)
        self.assertEqual(readback, result)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["failure"], {"kind": "tls", **failure.details})
        self.assertFalse(result["associated"])
        self.assertFalse(result["promotion"])


class Provenance(unittest.TestCase):
    def observation(self, role="[prove] native binary", step="[prove] native binary"):
        self.job = {"id": 42, "name": role, "run_id": 7, "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "main", "status": "completed", "conclusion": "success", "started_at": "2026-11-04T05:00:00Z", "steps": [{"name": step, "status": "completed", "conclusion": "success", "started_at": "2026-11-04T05:00:00Z"}]}
        self.document = {"total_count": 1, "jobs": [self.job]}
        self.native = run("completed")

        def api(path):
            if path == "":
                return REPOSITORY
            if path == "branches/main":
                return {"name": "main", "protected": True}
            if path.endswith("/jobs?per_page=100"):
                return self.document
            return self.native

        return api

    def test_successful_proof_step_is_distinct_from_whole_workflow_success(self):
        api = self.observation()
        self.native["conclusion"] = "failure"
        with mock.patch.object(control, "api", side_effect=api) as called:
            self.assertEqual(native.issued(IDENTITY), (IDENTITY, self.job["started_at"]))
        called.assert_any_call("actions/runs/7/attempts/1/jobs?per_page=100")
        self.assertFalse(any("/attempts/2" in call.args[0] for call in called.call_args_list))

    def test_missing_ambiguous_or_unsuccessful_issuer_roles_refuse(self):
        cases = [{"name": "foreign"}, {"status": "queued"}, {"steps": None}, {"steps": []}, {"steps": [{"name": "[prove] native binary", "status": "completed", "conclusion": "failure"}]}, {"steps": [{"name": "foreign", "status": "completed", "conclusion": "success"}]}]
        for changed in cases:
            api = self.observation()
            self.job.update(changed)
            with self.subTest(changed=changed), mock.patch.object(control, "api", side_effect=api), self.assertRaises(Refusal):
                native.issued(IDENTITY)

        api = self.observation()
        self.document["jobs"].append(dict(self.job, id=43))
        self.document["total_count"] = 2
        with mock.patch.object(control, "api", side_effect=api), self.assertRaisesRegex(Refusal, "ambiguous"):
            native.issued(IDENTITY)

    def test_foreign_job_attempt_source_or_truncated_catalog_refuses(self):
        for changed in ({"run_attempt": 2}, {"run_id": 8}, {"head_sha": "b" * 40}, {"head_branch": "feature/331"}, {"id": True}):
            api = self.observation()
            self.job.update(changed)
            with self.subTest(changed=changed), mock.patch.object(control, "api", side_effect=api), self.assertRaisesRegex(Refusal, "catalog identity"):
                native.issued(IDENTITY)
        api = self.observation()
        self.document["total_count"] = 2
        with mock.patch.object(control, "api", side_effect=api), self.assertRaisesRegex(Refusal, "catalog is incomplete"):
            native.issued(IDENTITY)

    def test_issuance_requires_the_fixed_completed_proof_step(self):
        api = self.observation("[prove] native binary", "[prove] native binary")
        self.job.update(status="in_progress", conclusion=None)
        with mock.patch.object(control, "api", side_effect=api):
            self.assertEqual(native.issued(IDENTITY), (IDENTITY, self.job["started_at"]))
        for changed in ({"status": "in_progress", "conclusion": None}, {"status": "completed", "conclusion": "failure"}, {"name": "[read] native binary"}, {"started_at": None}):
            api = self.observation("[prove] native binary", "[prove] native binary")
            self.job["steps"][0].update(changed)
            with self.subTest(changed=changed), mock.patch.object(control, "api", side_effect=api), self.assertRaises(Refusal):
                native.issued(IDENTITY)


class Declarations(unittest.TestCase):
    def test_altered_private_plan_is_rejected_before_release_writer(self):
        world = World()
        world.plan["context"]["native_declaration"]["model"]["windows"] = "machine"
        world.blob.objects[plan.location(world.plan["context"])] = canonical.encode(world.plan)
        with world.wired(), mock.patch.object(producer.r2, "writer", side_effect=AssertionError("release writer")), self.assertRaisesRegex(Refusal, "referenced plan bytes"):
            producer.produce(world.held)
        self.assertFalse(world.public.objects)

    def test_missing_public_plan_reference_has_no_historical_source_fallback(self):
        world = World()
        del world.distribution["plan"]
        world.documents[AUTHORITY + "/" + world.root + "/distribution.json"] = canonical.encode(world.distribution)
        with world.wired(), mock.patch.object(producer.r2, "configured", side_effect=AssertionError("private plan read")), self.assertRaisesRegex(Refusal, "exact publication plan reference"):
            producer.produce(world.held)
        self.assertFalse(world.public.objects)

    def test_public_declaration_needs_actual_successful_issuer_step(self):
        world = World()
        with world.wired():
            producer.produce(world.held)
        original = world.api

        def changed(path):
            value = original(path)
            if path == f"actions/runs/{world.observer['run']}/attempts/1/jobs?per_page=100":
                value["jobs"][0]["steps"][0]["conclusion"] = "failure"
            return value

        with world.wired(), mock.patch.object(control, "api", side_effect=changed), self.assertRaisesRegex(Refusal, "proof step did not succeed"):
            public.check(world.held)
