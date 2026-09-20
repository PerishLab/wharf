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


class Reading(unittest.TestCase):
    def stored(self, entries):
        bucket = Memory()
        plan.record(bucket, CONTEXT, entries, {})
        return bucket

    def test_a_job_reads_the_plan_of_its_own_run(self):
        document = plan.read(self.stored(ENTRIES), CONTEXT)
        self.assertEqual(document["entries"]["binary-linux"]["key"], "d" * 64)

    def test_the_product_name_comes_from_the_repository_the_plan_names(self):
        self.assertEqual(plan.product(CONTEXT), "plumb")

    def test_a_document_of_another_schema_is_refused(self):
        bucket = self.stored(ENTRIES)
        bucket.put(plan.location(CONTEXT), json.dumps({"schema": 2, "entries": {}}).encode())
        with self.assertRaisesRegex(Refusal, "not a schema 1 plan"):
            plan.read(bucket, CONTEXT)

    def test_an_entry_the_plan_never_decided_refuses(self):
        with self.assertRaisesRegex(Refusal, "holds no entry binary-macos"):
            plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "binary-macos")

    def test_an_entry_the_plan_decided_to_skip_refuses_to_be_run(self):
        with self.assertRaisesRegex(Refusal, "decided 'skip' for npm"):
            plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "npm")

    def test_an_entry_the_plan_decided_to_run_is_handed_back(self):
        entry = plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "binary-linux")
        self.assertEqual(entry, {"key": "d" * 64, "decision": "run"})


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


