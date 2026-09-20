import unittest

from lib import parameters
from lib.refusal import Refusal
from scripts import plan

STEPS = """
{
  "binary-linux": {"outputs": {"key": "aaa", "decision": "run"}},
  "npm": {"outputs": {"decision": "skip"}},
  "source": {"outputs": {"commit": "ccc"}}
}
"""
PLANNED = {"binary-linux": {"key": "aaa", "decision": "run"}, "npm": {"decision": "skip"}}


class Taken(unittest.TestCase):
    def test_every_parameter_this_script_takes_is_declared(self):
        self.assertEqual([name for name in plan.TAKEN if name not in parameters.TYPES], [])

    def test_the_action_word_is_taken_before_the_parameters(self):
        self.assertEqual(parameters.acted("plan", plan.ACTIONS, ["record", "--marker", "v1"]), ("record", ["--marker", "v1"]))

    def test_an_unknown_action_refuses(self):
        with self.assertRaisesRegex(Refusal, "plan takes one action: record"):
            parameters.acted("plan", plan.ACTIONS, ["invent"])

    def test_the_product_name_comes_from_the_repository(self):
        self.assertEqual(plan.named("PerishLab/plumb"), "plumb")

    def test_a_repository_that_is_not_owner_and_name_refuses(self):
        with self.assertRaisesRegex(Refusal, "is not owner/name"):
            plan.named("plumb")

    def test_every_build_target_states_its_name_target_and_runner(self):
        self.assertEqual(sorted(plan.BUILD["targets"][0]), ["name", "runner", "target"])
        self.assertEqual([target["name"] for target in plan.BUILD["targets"]], ["linux", "windows", "macos"])


class Reported(unittest.TestCase):
    def test_only_the_fields_a_plan_states_are_read_back(self):
        held = plan.reported(STEPS)
        self.assertEqual(held["binary-linux"], {"key": "aaa", "decision": "run"})
        self.assertEqual(held["source"], {})

    def test_an_absent_steps_context_reads_as_nothing(self):
        self.assertEqual(plan.reported(""), {})


class Media(unittest.TestCase):
    def test_a_credentialed_check_is_recorded_as_its_step_reported_it(self):
        entries = {}
        observed = {name: {"decision": "skip"} for name in plan.MEDIA}
        plan.media(observed, entries)
        self.assertEqual(sorted(entries), sorted(plan.MEDIA))
        self.assertEqual(entries["npm"], {"decision": "skip"})

    def test_a_step_that_reported_nothing_refuses_the_record(self):
        observed = {name: {"decision": "run"} for name in plan.MEDIA if name != "oci"}
        with self.assertRaisesRegex(Refusal, "oci"):
            plan.media(observed, {})


class Settled(unittest.TestCase):
    def test_agreement_passes_quietly(self):
        plan.settled(PLANNED, plan.reported(STEPS))

    def test_disagreement_names_the_entry_and_refuses(self):
        drifted = dict(PLANNED, npm={"decision": "run"})
        with self.assertRaises(Refusal) as held:
            plan.settled(drifted, plan.reported(STEPS))
        self.assertIn("npm.decision", str(held.exception))
