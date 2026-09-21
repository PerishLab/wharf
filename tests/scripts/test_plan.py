import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import parameters
from lib.process import git
from lib.refusal import Refusal
from scripts import plan
from tests.lib.store.memory import Memory

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
        with self.assertRaisesRegex(Refusal, "plan takes one action: check, record"):
            parameters.acted("plan", plan.ACTIONS, ["invent"])

    def test_every_build_target_states_its_name_target_and_runner(self):
        self.assertEqual(sorted(plan.BUILD["targets"][0]), ["name", "runner", "target"])
        self.assertEqual([target["name"] for target in plan.BUILD["targets"]], ["linux", "windows", "macos"])


class Checked(unittest.TestCase):
    def test_a_well_formed_request_names_its_channel(self):
        self.assertEqual(plan.check({"repository": "PerishLab/plumb", "marker": "v0.38.3-rc.7"})["channel"], "rc")

    def test_a_malformed_marker_refuses(self):
        with self.assertRaisesRegex(Refusal, "not a release marker"):
            plan.check({"repository": "PerishLab/plumb", "marker": "v0.38.3-rc.0"})

    def test_the_repository_shape_is_enforced_by_the_parameter_layer(self):
        self.assertIn("repository", parameters.SHAPES_DECLARED)
        self.assertTrue(parameters.SHAPES_DECLARED["repository"].fullmatch("PerishLab/plumb"))


class Answered(unittest.TestCase):
    ENTRIES = {
        "binary-linux": {"key": "a" * 64, "decision": "run"},
        "binary-macos": {"key": "b" * 64, "decision": "skip"},
        "suite-linux": {"key": "c" * 64, "decision": "run"},
        "cfworker": {"key": "d" * 64, "decision": "skip"},
    }

    def answered(self, entries):
        path = Path(tempfile.mkdtemp()) / "output"
        path.write_text("")
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(path)}):
            named = plan.emit(entries, "3")
        return named, path.read_text()

    def test_the_layers_and_single_jobs_are_written_where_the_runner_reads_them(self):
        named, written = self.answered(self.ENTRIES)
        self.assertEqual([unit["name"] for unit in json.loads(named["layer-1"])], ["binary linux", "suite"])
        self.assertEqual(named["cfworker"], "skip")
        self.assertIn("planned=3", written)
        self.assertEqual([line.split("=")[0] for line in written.splitlines() if line.startswith("layer-")], ["layer-1", "layer-2", "layer-3"])
        self.assertEqual(len(written.splitlines()), len(plan.SINGLE) + plan.UNITS["layers"] + 1)

    def test_a_product_with_no_worker_plans_nothing_for_it(self):
        named, _ = self.answered({"suite-linux": {"key": "a" * 64, "decision": "run"}})
        self.assertEqual(named["cfworker"], "skip")

    def test_nowhere_to_answer_the_caller_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(Refusal, "GITHUB_OUTPUT is not set"):
                parameters.answer({})


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


class Derived(unittest.TestCase):
    HELD = {"repository": "PerishLab/plumb", "marker": "v0.38.3-rc.7", "commit": "a" * 40, "tree": "b" * 40, "source": "../product"}

    def entries(self):
        entries = {}
        patches = (
            mock.patch.object(plan.basis, "resolve", side_effect=lambda source, name, target, runner: {"entry": "binary", "target": target}),
            mock.patch.object(plan.basis, "dependencies", side_effect=lambda source, name, target, runner: {"entry": "dependencies", "target": target}),
            mock.patch.object(plan.basis, "suite", return_value={"entry": "suite"}),
            mock.patch.object(plan.node, "carried", return_value=False),
            mock.patch.object(plan.cfworker, "workers", return_value=[]),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            plan.binaries(self.HELD, Memory(), entries)
            plan.suites(self.HELD, Memory(), entries)
        return entries

    def test_an_entry_consumes_exactly_the_entries_whose_keys_its_basis_names(self):
        consumed = {name: entry.get("consumes", []) for name, entry in self.entries().items()}
        for target in ("linux", "windows", "macos"):
            self.assertEqual(consumed[f"bind-{target}"], [f"binary-{target}"])
            self.assertEqual(consumed[f"smoke-{target}"], [f"bind-{target}"])
            self.assertEqual(consumed[f"binary-{target}"], [])
            self.assertEqual(consumed[f"dependencies-{target}"], [])
        self.assertEqual(consumed["suite-linux"], [])

    def test_a_layer_runs_one_unit_per_job_and_names_what_each_must_prepare(self):
        held = plan.units(self.entries())
        self.assertEqual([unit["name"] for unit in held["layer-1"]], ["binary linux", "binary macos", "binary windows", "suite"])
        self.assertEqual(held["layer-1"][2]["prepare"], ["autocrlf"])
        self.assertEqual(held["layer-2"], [{"name": "bind", "action": "bind", "target": "", "runner": plan.RUNNER, "prepare": ["rcodesign"]}])
        self.assertEqual([unit["name"] for unit in held["layer-3"]], ["smoke linux", "smoke macos", "smoke windows"])
        windows = held["layer-3"][2]
        self.assertEqual((windows["target"], windows["runner"], windows["prepare"]), ("x86_64-pc-windows-msvc", "windows-2025", ["autocrlf"]))

    def test_a_node_workspace_runs_its_suite_in_the_first_layer_with_node_and_pnpm_prepared(self):
        entries = self.entries()
        entries["suite-node"] = {"key": "e" * 64, "decision": "run"}
        suite = [unit for unit in plan.units(entries)["layer-1"] if unit["action"] == "node-suite"]
        self.assertEqual(suite, [{"name": "node-suite", "action": "node-suite", "target": "", "runner": plan.RUNNER, "prepare": ["node", "pnpm"]}])

    def test_a_unit_the_plan_decided_to_skip_is_absent_from_its_layer(self):
        entries = self.entries()
        for name in ("smoke-linux", "smoke-macos", "bind-linux", "bind-macos", "bind-windows"):
            entries[name]["decision"] = "skip"
        held = plan.units(entries)
        self.assertEqual(held["layer-2"], [])
        self.assertEqual([unit["name"] for unit in held["layer-3"]], ["smoke windows"])

    def test_a_plan_deeper_than_the_workflow_refuses(self):
        entries = {f"step-{number}": {"key": str(number) * 64, "decision": "run", **({"consumes": [f"step-{number - 1}"]} if number else {})} for number in range(4)}
        with self.assertRaisesRegex(Refusal, "derived 4 layers, the workflow runs 3"):
            plan.units(entries)

    def test_every_consumed_entry_is_already_a_need_of_the_job_that_consumes_it(self):
        needs = workflow()
        layered = {"binary": "layer-1", "dependencies": "layer-1", "suite": "layer-1", "bind": "layer-2", "smoke": "layer-3"}
        job = lambda name: layered.get(name.split("-")[0], name)
        for name, entry in self.entries().items():
            for consumed in entry.get("consumes", []):
                self.assertIn(job(consumed), needs[job(name)], f"{job(name)} consumes {job(consumed)}")


def workflow():
    needs, current = {}, None
    for line in (Path(git(".", "rev-parse", "--show-toplevel")) / ".github/workflows/ship.yml").read_text().splitlines():
        if re.fullmatch(r"  [a-z0-9-]+:", line):
            current = line.strip().rstrip(":")
            needs[current] = []
        elif current and line.startswith("    needs:"):
            needs[current] = re.findall(r"[a-z0-9-]+", line.split(":", 1)[1])
    return needs
