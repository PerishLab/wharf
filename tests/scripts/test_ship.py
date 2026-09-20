import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout

from unittest import mock

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


class Building(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.basis = {"entry": {"kind": "cargo-binary", "binary": "plumb"}}
        self.key = ship.workload.key(self.basis, ship.implementation.resourced(["lib.cargo.basis", "lib.cargo.build"], []))
        self.held = dict(CONTEXT, source="../product", target="x86_64-unknown-linux-gnu")

    def stored(self, entries):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})

    def built(self, produced):
        def build(request):
            Path(request.output).joinpath("plumb").write_bytes(produced)
            return {"artifact": "plumb"}

        return mock.patch.object(ship.build, "build", side_effect=build)

    def running(self):
        return mock.patch.object(ship.r2, "configured", return_value=self.bucket), mock.patch.object(ship.basis, "resolve", return_value=self.basis)

    def run_binary(self):
        bucket, resolve = self.running()
        with bucket, resolve, self.built(b"an executable"):
            return ship.run_binary(self.held)

    def test_the_binary_is_recorded_under_the_key_the_plan_holds(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "run"}})
        self.assertEqual(self.run_binary(), {"key": self.key, "state": "recorded", "files": {"plumb": {"size": 13, "sha256": mock.ANY}}})
        self.assertIn(f"workload/1/{self.key}/record.json", self.bucket.objects)

    def test_the_basis_recorded_is_the_one_the_key_was_resolved_from(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "run"}})
        self.run_binary()
        self.assertEqual(json.loads(self.bucket.objects[f"workload/1/{self.key}/basis.json"]), self.basis)

    def test_the_context_recorded_is_the_run_that_asked_for_it(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "run"}})
        self.run_binary()
        record = json.loads(self.bucket.objects[f"workload/1/{self.key}/record.json"])
        self.assertEqual(record["context"], CONTEXT)

    def test_a_plan_that_decided_to_skip_stops_the_job(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "skip"}})
        with self.assertRaisesRegex(Refusal, "decided 'skip' for binary-linux"):
            self.run_binary()

    def test_a_plan_holding_another_key_stops_the_job_before_it_records_anything(self):
        self.stored({"binary-linux": {"key": "d" * 64, "decision": "run"}})
        with self.assertRaisesRegex(Refusal, "the plan for this run recorded"):
            self.run_binary()
        self.assertEqual([name for name in self.bucket.objects if name.startswith("workload/")], [])


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
