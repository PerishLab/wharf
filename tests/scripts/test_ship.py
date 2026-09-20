import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from lib import parameters
from lib.refusal import Refusal
from lib.store import plan
from scripts import ship
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/plumb", "marker": "v0.38.3-rc.4", "wharf": "c" * 40, "run": "35496615732", "attempt": "1"}


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in ship.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())

    def test_a_target_this_repository_does_not_build_refuses(self):
        with self.assertRaisesRegex(Refusal, "not one this repository builds"):
            ship.targeted("s390x-unknown-linux-gnu")

    def test_a_target_carries_the_name_the_plan_uses_and_the_runner_it_builds_on(self):
        self.assertEqual(ship.targeted("x86_64-pc-windows-msvc"), {"name": "windows", "target": "x86_64-pc-windows-msvc", "runner": "windows-2025"})


class Planned(unittest.TestCase):
    def stored(self, entries):
        bucket = Memory()
        plan.record(bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})
        return bucket

    def test_a_job_whose_basis_resolves_to_another_key_refuses_to_publish_under_the_planned_one(self):
        entry = {"key": "d" * 64, "decision": "run"}
        with self.assertRaisesRegex(Refusal, "the plan for this run recorded"):
            ship.resolved(entry, {"entry": {"kind": "cargo-binary"}}, (["lib.cargo.build"], []))

    def test_a_job_whose_basis_resolves_to_the_planned_key_carries_on(self):
        held = {"entry": {"kind": "cargo-binary"}}
        key = ship.workload.key(held, ship.implementation.resourced(["lib.cargo.build"], []))
        self.assertEqual(ship.resolved({"key": key, "decision": "run"}, held, (["lib.cargo.build"], [])), key)


class Ship(unittest.TestCase):
    def test_refusal_exits_two(self):
        argv = ["smoke", "--dir", "/nonexistent", "--name", "demo", "--target", "x86_64-unknown-linux-gnu", "--output", "/nonexistent/out", "--expect", "demo v1.0.0"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            code = ship.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("refused", error.getvalue())

    def test_smoke_key_is_stable(self):
        output = io.StringIO()
        with redirect_stdout(output):
            ship.main(["key-smoke", "--binary-key", "a" * 64, "--basis", "/dev/null"])
            ship.main(["key-smoke", "--binary-key", "a" * 64, "--basis", "/dev/null"])
        keys = [line for line in output.getvalue().splitlines() if '"key"' in line]
        self.assertEqual(len(set(keys)), 1)
