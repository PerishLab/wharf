import json
import unittest

from lib.refusal import Refusal
from lib.store import trigger
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/plumb", "marker": "v0.38.0-beta.1", "run": "7", "attempt": "1"}


class Trigger(unittest.TestCase):
    def test_complete_when_every_job_settled(self):
        bucket = Memory()
        result = trigger.record(bucket, CONTEXT, {"plan": {"result": "success"}, "binary-linux": {"result": "skipped"}})
        self.assertEqual(result, {"record": "trigger/PerishLab/plumb/v0.38.0-beta.1/7-1.json", "state": "complete"})
        self.assertEqual(json.loads(bucket.get(result["record"]))["state"], "complete")

    def test_incomplete_when_a_job_failed(self):
        result = trigger.record(Memory(), CONTEXT, {"plan": {"result": "success"}, "binary-linux": {"result": "failure"}})
        self.assertEqual(result["state"], "incomplete")

    def test_refuses_malformed_marker(self):
        with self.assertRaises(Refusal):
            trigger.record(Memory(), dict(CONTEXT, marker="latest"), {})

    def test_records_rc_and_stable_markers(self):
        for marker in ("v0.38.0-rc.1", "v0.38.0"):
            result = trigger.record(Memory(), dict(CONTEXT, marker=marker), {"plan": {"result": "success"}})
            self.assertEqual(result["record"], f"trigger/PerishLab/plumb/{marker}/7-1.json")
