import json
import unittest

from lib.refusal import Refusal
from lib.store import plan
from tests.lib.store.memory import Memory

CONTEXT = {
    "repository": "PerishLab/plumb",
    "marker": "v0.38.3",
    "commit": "a" * 40,
    "tree": "b" * 40,
    "wharf": "c" * 40,
    "run": "35493492267",
    "attempt": "1",
}
ENTRIES = {"binary-linux": {"key": "d" * 64, "decision": "run"}, "npm": {"decision": "skip"}}


class Address(unittest.TestCase):
    def test_a_run_owns_its_plan_and_the_marker_keeps_a_pointer(self):
        self.assertEqual(plan.location(CONTEXT), "plan/PerishLab/plumb/v0.38.3/35493492267-1.json")
        self.assertEqual(plan.standing(CONTEXT), "plan/PerishLab/plumb/v0.38.3/latest.json")

    def test_a_malformed_request_is_refused_before_anything_is_written(self):
        for field, value in (("repository", "plumb"), ("marker", "0.38.3")):
            with self.assertRaises(Refusal):
                plan.location(dict(CONTEXT, **{field: value}))


class Record(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def test_the_run_object_is_immutable_and_the_pointer_follows_it(self):
        held = plan.record(self.bucket, CONTEXT, ENTRIES, {"node": "24.18.0"})
        self.assertEqual((held["entries"], held["plan"]), (2, plan.location(CONTEXT)))
        self.assertEqual(self.bucket.get(plan.standing(CONTEXT)), self.bucket.get(plan.location(CONTEXT)))
        document = json.loads(self.bucket.get(plan.location(CONTEXT)))
        self.assertEqual(list(document["entries"]), ["binary-linux", "npm"])
        self.assertEqual(document["context"]["marker"], "v0.38.3")
        with self.assertRaises(Exception):
            plan.record(self.bucket, CONTEXT, ENTRIES, {"node": "24.18.0"})

    def test_a_second_run_of_one_marker_records_its_own_plan(self):
        plan.record(self.bucket, CONTEXT, ENTRIES, {})
        later = dict(CONTEXT, attempt="2")
        plan.record(self.bucket, later, {"npm": {"decision": "run"}}, {})
        self.assertNotEqual(self.bucket.get(plan.location(CONTEXT)), self.bucket.get(plan.location(later)))
        self.assertEqual(self.bucket.get(plan.standing(CONTEXT)), self.bucket.get(plan.location(later)))


class Agreement(unittest.TestCase):
    def test_a_step_that_reported_another_key_is_named(self):
        observed = {"binary-linux": {"key": "e" * 64, "decision": "run"}, "npm": {"decision": "skip"}}
        drift = plan.agreed(ENTRIES, observed)
        self.assertEqual(len(drift), 1)
        self.assertIn("binary-linux.key", drift[0])

    def test_a_missing_step_is_drift_and_agreement_is_silent(self):
        self.assertTrue(plan.agreed(ENTRIES, {"npm": {"decision": "skip"}}))
        self.assertEqual(plan.agreed(ENTRIES, ENTRIES), [])
