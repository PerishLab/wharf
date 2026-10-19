import unittest
from pathlib import Path

from lib.process.lane import entry


class Intake(unittest.TestCase):
    def test_reusable_composition_exposes_no_partial_dispatch_entry(self):
        root = Path(entry.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/intake.yml").read_text()
        self.assertIn("\n  workflow_call:\n", workflow)
        for trigger in ("workflow_dispatch:", "repository_dispatch:", "push:", "schedule:", "pull_request:"):
            self.assertNotIn(trigger, workflow)
        self.assertEqual(entry.policy()["workflow"], ".github/workflows/lane.yml")
        self.assertNotIn("\n  apply:\n", workflow)
        self.assertNotIn("\n  dispose:\n", workflow)
        self.assertNotIn("lane-observe", workflow)
        self.assertNotIn("lane-apply", workflow)
        self.assertNotIn("lane-dispose", workflow)
        self.assertNotIn("WHARF_LANE_EVIDENCE_", workflow)
        self.assertNotIn("WHARF_LANE_PROVIDER_", workflow)
        self.assertNotIn("WHARF_RELEASES_", workflow)

    def test_outputs_name_only_producing_job_observations(self):
        root = Path(entry.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/intake.yml").read_text()
        interface = workflow.split("permissions: {}", 1)[0]
        for value in ("jobs.admission.outputs.operation", "jobs.admission.outputs.admitted", "jobs.build.outputs.workload",
                      "jobs.build.outputs.artifact", "jobs.reserve.outputs.prepared", "jobs.consume.outputs.consumed"):
            self.assertIn(value, interface)
        for value in ("steps.token.outputs.token", "product-token", "result", "published", "settled"):
            self.assertNotIn(value, interface)
        self.assertIn("group: wharf-static-lane-intake", workflow)
        self.assertNotIn("group: wharf-static-lane\n", workflow)


if __name__ == "__main__":
    unittest.main()
