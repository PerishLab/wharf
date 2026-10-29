import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical
from lib.process.trial import context, credential, publication, transport
from lib.refusal import Refusal
from tests.scripts.test_distribution.world import AUTHORITY, OBSERVER, STARTED, World, producer


class Qualification(unittest.TestCase):
    def test_trusted_full_suite_issues_only_exact_linux_combination(self):
        world = World()
        with tempfile.TemporaryDirectory() as directory, world.wired():
            evidence = world.write(directory)
            result, passed = credential.assessment(world.prepared, evidence, producer())
        self.assertTrue(result["ok"])
        self.assertEqual(passed["requirements"]["platform"], "linux-x86_64")
        self.assertEqual(passed["source"], world.source)
        self.assertEqual(passed["declaration"], world.distribution["trial"])
        self.assertEqual(passed["images"], world.report()["images"])
        self.assertEqual(passed["groups"], world.report()["groups"])

    def test_failed_cleanup_missing_case_report_swap_and_stale_inputs_never_issue(self):
        world = World()
        mutations = [{"cleanup": False}, {"cases": []}, {"products": []}, {"runner": "0" * 64}, {"scene": dict(world.scene, generation="0" * 64)}, {"ok": False}, {"groups": []}]
        with tempfile.TemporaryDirectory() as directory, world.wired():
            evidence = world.write(directory)
            for change in mutations:
                (evidence / "report.json").write_bytes(canonical.encode(dict(world.report(), **change)))
                with self.subTest(change=change):
                    result, passed = credential.assessment(world.prepared, evidence, producer())
                    self.assertFalse(result["ok"])
                    self.assertIsNone(passed)
            world.write(directory)
            for job in (dict(producer(), conclusion="failure"), dict(producer(), conclusion="cancelled"), dict(producer(), steps=[])):
                result, passed = credential.assessment(world.prepared, evidence, job)
                self.assertFalse(result["ok"])
                self.assertIsNone(passed)
            with mock.patch.object(credential.preparation, "unchanged", side_effect=Refusal("scene moved")):
                self.assertIsNone(credential.assessment(world.prepared, evidence, producer())[1])

    def test_diagnostic_success_cannot_issue_suite_qualification(self):
        world = World()
        prepared = dict(world.prepared, requirements=dict(world.prepared["requirements"], scope="diagnostic"))
        with tempfile.TemporaryDirectory() as directory, world.wired():
            self.assertIsNone(credential.assessment(prepared, world.write(directory), producer())[1])

    def test_cross_job_blob_delivery_and_independent_publication(self):
        world = World()
        roles = {context.NAMES["windtun-prepare"]: {"status": "completed", "conclusion": "success", "started_at": STARTED, "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in ("[windtun] admit publisher", "[windtun] prepare attempt")]}, context.NAMES["windtun"]: producer()}
        held = {"observer": json.dumps(OBSERVER), "repository": "PerishLab/plumb", "marker": world.source["marker"], "trialcase": "declared"}
        with tempfile.TemporaryDirectory() as directory, world.wired(), mock.patch.object(context, "observe", return_value=(OBSERVER, roles)):
            root = Path(directory)
            world.write(root / "prepare/evidence")
            transport.prepare(dict(held, source=str(root / "prepare")))
            transport.load(dict(held, source=str(root / "executor")))
            world.write(root / "executor/evidence")
            material = transport.store(dict(held, source=str(root / "executor")))
            result = credential.issue(dict(held, digest=material["digest"]))
            self.assertTrue(result["ok"])
            passed = publication.verified(result["credential"], AUTHORITY)
            self.assertEqual(credential.current(world.prepared), passed)
            self.assertEqual(passed["evidence"]["key"], material["key"])
            self.assertEqual(publication.read(held["repository"], held["marker"], OBSERVER)["preparation"]["key"], context.address(world.prepared, "preparation"))
            with self.assertRaises(Refusal):
                credential.issue(dict(held, digest="0" * 64))
            pending = dict(world.prepared, observer=dict(OBSERVER, attempt="2"))
            publication.begin(world.public, AUTHORITY, pending, {name: "a" * 64 for name in ("key", "record", "basis")})
            with self.assertRaises(Refusal):
                credential.current(world.prepared)

    def test_cancelled_without_collection_publishes_unknown_not_a_pass(self):
        world = World()
        roles = {context.NAMES["windtun-prepare"]: {"status": "completed", "conclusion": "success", "started_at": STARTED, "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in ("[windtun] admit publisher", "[windtun] prepare attempt")]}, context.NAMES["windtun"]: {"status": "completed", "conclusion": "cancelled"}}
        held = {"observer": json.dumps(OBSERVER), "repository": "PerishLab/plumb", "marker": world.source["marker"], "trialcase": "declared"}
        with tempfile.TemporaryDirectory() as directory, world.wired(), mock.patch.object(context, "observe", return_value=(OBSERVER, roles)):
            world.write(Path(directory) / "evidence")
            transport.prepare(dict(held, source=directory))
            result = credential.issue(dict(held, digest="null"))
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(result["credential"])
