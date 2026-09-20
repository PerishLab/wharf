import json
import unittest
from unittest import mock

from lib.store import plan
from scripts import store
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/plumb", "marker": "v0.38.3-rc.8", "wharf": "c" * 40, "run": "35498486129", "attempt": "1", "actor": "someone"}
NEEDS = json.dumps({"plan": {"result": "success", "outputs": {}}, "release": {"result": "skipped"}})


class Trigger(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.held = dict(CONTEXT, needs=NEEDS)

    def written(self):
        with mock.patch.object(store.r2, "configured", return_value=self.bucket):
            return store.record(self.held)

    def recorded(self):
        return json.loads(self.bucket.objects["trigger/PerishLab/plumb/v0.38.3-rc.8/35498486129-1.json"])

    def test_the_commit_and_tree_come_from_the_plan_this_run_wrote(self):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), {}, {})
        self.written()
        self.assertEqual(self.recorded()["context"]["commit"], "a" * 40)

    def test_a_run_whose_plan_was_never_written_still_owes_its_trigger_record(self):
        self.held["needs"] = json.dumps({"plan": {"result": "failure"}})
        self.assertEqual(self.written()["state"], "incomplete")
        self.assertIsNone(self.recorded()["context"]["commit"])

    def test_every_job_result_is_kept(self):
        self.written()
        self.assertEqual(sorted(self.recorded()["jobs"]), ["plan", "release"])

    def test_every_parameter_the_trigger_takes_is_declared(self):
        names = [name for _, names in store.ACTIONS.values() for name in names]
        self.assertEqual([name for name in names if name not in store.parameters.TYPES], [])


if __name__ == "__main__":
    unittest.main()
