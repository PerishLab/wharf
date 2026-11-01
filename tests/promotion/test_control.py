import copy
import os
import unittest
from unittest import mock

from lib.process.promotion import control, operation, subject
from lib.refusal import Refusal
from tests.promotion.world import World


class Control(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)

    def test_foreign_native_role_or_duplicate_job_cannot_write_material(self):
        w = self.world
        with w.wired(), mock.patch.dict(os.environ, {"GITHUB_JOB": "build-candidate"}), self.assertRaises(Refusal):
            operation.prepare(w.held)
        before = list(w.world.blob.writes)
        w.jobs.append(copy.deepcopy(w.jobs[0]))
        with w.wired(), self.assertRaises(Refusal):
            operation.prepare(w.held)
        self.assertEqual(w.world.blob.writes, before)

    def test_preparation_must_precede_all_publication_starts(self):
        w = self.world
        w.jobs.append(dict(w.jobs[0], id=31, name="[publish] cargo", status="completed", conclusion="success"))
        with w.wired(), self.assertRaisesRegex(Refusal, "publication start"):
            operation.prepare(w.held)

    def test_bad_tag_intent_or_foreign_plan_is_negative_not_fallback(self):
        w = self.world
        for message in ("v1.2.3", "v1.2.3\n\npromotes v1.2.3-rc.1\npromotes v1.2.3-rc.2", "v1.2.3\n\npromotes v1.2.4-rc.1"):
            w.message = message
            with self.subTest(message=message), w.wired(), self.assertRaises(Refusal):
                subject.resolve(w.world.blob, {"repository": "PerishLab/demo", "marker": "v1.2.3", "run": "8", "attempt": "1"}, w.world.observer)
        with w.wired(), self.assertRaisesRegex(Refusal, "replay"):
            subject.resolve(w.world.blob, {"repository": "PerishLab/demo", "marker": "v1.2.3", "run": "9", "attempt": "1"}, w.world.observer)

    def test_stable_intent_rechecks_annotated_tag_identity(self):
        w = self.world
        with w.wired():
            _, _, original = subject.resolve(w.world.blob, {"repository": "PerishLab/demo", "marker": "v1.2.3", "run": "8", "attempt": "1"}, w.world.observer)
            w.native = "f" * 40
            with self.assertRaisesRegex(Refusal, "moved"):
                subject.unchanged(original)
