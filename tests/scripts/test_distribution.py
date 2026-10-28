import hashlib
import json
import tempfile
from pathlib import Path
import unittest

from unittest import mock

from lib import parameters
from scripts import distribution
from lib.process.trial import admission, consumer, judgment
from lib.refusal import Refusal

HELD = {"repository": "PerishLab/plumb", "marker": "v0.42.0", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1", "needs": json.dumps({"plan": {"result": "success"}})}


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in distribution.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())


class Record(unittest.TestCase):
    def test_the_record_is_written_to_the_product_release_bucket_and_read_back_from_its_authority(self):
        seen = {}

        def record(bucket, context, needs, reader):
            seen.update(bucket=bucket, context=context, needs=needs)
            with mock.patch.object(distribution.release, "fetch", side_effect=lambda url: url):
                seen["read"] = reader("v1/releases/stable/v0.42.0/distribution.json")
            return {"state": "complete"}

        with mock.patch.object(distribution.r2, "writer", side_effect=lambda name, role: (name, role)), mock.patch.object(distribution.distribution, "record", side_effect=record):
            self.assertEqual(distribution.record(HELD), {"state": "complete"})
        self.assertEqual(seen["bucket"], ("perish-plumb-releases", "RELEASES"))
        self.assertEqual(seen["read"], "https://releases.plumb.perish.uk/v1/releases/stable/v0.42.0/distribution.json")
        self.assertEqual((seen["context"]["commit"], seen["needs"]), ("a" * 40, {"plan": {"result": "success"}}))


def publisher():
    return {"id": 7, "run_attempt": 1, "repository": {"full_name": "PerishLab/wharf"}, "head_repository": {"full_name": "PerishLab/wharf"}, "event": "workflow_dispatch", "path": ".github/workflows/ship.yml", "head_branch": "main", "head_sha": "a" * 40, "status": "completed", "display_title": "[ship] PerishLab/plumb v1.2.3-rc.1"}


def report():
    return {"schema": "seatrial.run/v1", "platform": "linux-x86_64", "runner": "a" * 64, "ok": True, "state": "passed", "cleanup": True, "selected": ["writer"], "cases": [{"name": "writer", "passed": True, "error": None, "seconds": 1}], "seconds": 9, "stages": [{"name": name, "start": index, "seconds": 0.5, "ok": True} for index, name in enumerate(judgment.STAGES)], "scene": {"product": "seatrial", "kind": "scene", "channel": "stable"}, "images": [{"name": "plumb", "digest": "ghcr.io/perishlab/images@sha256:" + "b" * 64}]}


class Admission(unittest.TestCase):
    def test_only_exact_trusted_nonstable_publisher_is_eligible(self):
        run = publisher()
        self.assertEqual(admission.candidate(run, "7"), "v1.2.3-rc.1")
        for key, value in (("id", 8), ("path", ".github/workflows/other.yml"), ("head_branch", "topic"), ("status", "in_progress"), ("event", "pull_request"), ("head_repository", {"full_name": "Else/wharf"})):
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.candidate(dict(run, **{key: value}), "7")
        self.assertIsNone(admission.candidate(dict(run, display_title="[ship] PerishLab/plumb v1.2.3"), "7"))
        self.assertIsNone(admission.candidate(dict(run, display_title="[ship] PerishLab/seatrial v1.2.3-rc.1"), "7"))

    def test_completion_belongs_to_exact_actual_attempt(self):
        run = publisher()
        held = {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1", "state": "complete", "commit": "b" * 40, "completed": {"run": "7", "attempt": "1", "wharf": "a" * 40}, "media": {"binaries": "published"}}
        self.assertEqual(admission.publication(run, held["marker"], held)["commit"], "b" * 40)
        for changed in ({"state": "incomplete"}, {"completed": dict(held["completed"], attempt="2")}, {"completed": None}, {"commit": ""}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                admission.publication(run, held["marker"], dict(held, **changed))


class Judgment(unittest.TestCase):
    def test_complete_success(self):
        judgment.complete(report(), {"exit": 0, "case": "writer", "scene_channel": "stable"}, {"sha256": "a" * 64})

    def test_failure_cancel_unknown_cleanup_and_incomplete_timing_refuse(self):
        mutations = [{"ok": False}, {"state": "cancelled"}, {"cleanup": None}, {"cleanup": False}, {"runner": "b" * 64}, {"cases": []}, {"selected": []}, {"stages": []}, {"seconds": float("nan")}, {"scene": None}, {"images": []}]
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                judgment.complete(dict(report(), **changed), {"exit": 0, "case": "writer", "scene_channel": "stable"}, {"sha256": "a" * 64})
        with self.assertRaises(Refusal):
            judgment.complete(report(), {"exit": 1, "case": "writer", "scene_channel": "stable"}, {"sha256": "a" * 64})

    def test_actual_product_archive_and_seal_must_match_publisher(self):
        seal = json.dumps({"product": "plumb", "channel": "rc", "releaseVersion": "v1.2.3-rc.1", "commit": "a" * 40, "artifacts": {"linux-x64": {"sha256": "b" * 64}}}).encode()
        expected = {"product": "plumb", "channel": "rc", "marker": "v1.2.3-rc.1", "commit": "a" * 40}
        product = {"name": "plumb", "channel": "rc", "version": expected["marker"], "commit": expected["commit"], "seal": hashlib.sha256(seal).hexdigest(), "archive": "b" * 64}
        judgment.identity({"products": [product]}, expected, seal)
        for field in ("version", "commit", "seal", "archive", "channel"):
            with self.subTest(field=field), self.assertRaises(Refusal):
                judgment.identity({"products": [dict(product, **{field: "different"})]}, expected, seal)

    def test_absent_report_preserves_unknown_and_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            (evidence / "admission.json").write_text('{"eligible":true}')
            (evidence / "execution.json").write_text('{"state":"executed"}')
            with self.assertRaises(FileNotFoundError):
                judgment.judge({"source": directory})
            self.assertEqual(json.loads((evidence / "observation.json").read_text())["state"], "unknown")

    def test_superseded_release_never_claims_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            (evidence / "admission.json").write_text('{"eligible":true,"channel":"rc","marker":"v1.2.3-rc.1"}')
            with mock.patch.object(consumer, "document", return_value={"releaseVersion": "v1.2.3-rc.2"}), mock.patch.object(consumer, "install") as install:
                result = consumer.execute({"source": directory, "trialcase": "writer"})
            install.assert_not_called()
            self.assertEqual(result["state"], "superseded")
            self.assertFalse(judgment.judge({"source": directory})["ok"])
