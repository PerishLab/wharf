import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from lib.refusal import Refusal
from lib.store import plan
from scripts import ship
from tests.lib.store.memory import Memory
from tests.scripts.test_ship import CONTEXT


class Inheriting(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.basis = {"entry": {"kind": "cargo-binary", "binary": "plumb"}}
        self.depended = {"entry": {"kind": "cargo-dependencies", "binary": "plumb"}}
        self.key = ship.workload.key(self.basis, ship.implementation.resourced(["lib.cargo.basis", "lib.cargo.build"], []))
        self.inherited = ship.workload.key(self.depended, ship.implementation.resourced(["lib.cargo.basis", "lib.cargo.build"], []))
        self.held = dict(CONTEXT, source="../product", target="x86_64-unknown-linux-gnu")
        self.restored = []
        self.archived = []

    def stored(self, decision):
        entries = {"binary-linux": {"key": self.key, "decision": "run"}, "dependencies-linux": {"key": self.inherited, "decision": decision}}
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})

    def seed(self):
        produced = Path(tempfile.mkdtemp()) / "produced"
        produced.mkdir(parents=True)
        produced.joinpath(ship.DEPENDENCIES).write_bytes(b"a packed target directory")
        ship.workload.publish(self.bucket, self.inherited, ship.workload.Produced(produced, self.depended, {}))

    def acting(self):
        def build(request):
            Path(request.output).mkdir(parents=True)
            Path(request.output).joinpath("plumb").write_bytes(b"an executable")
            return {"artifact": "plumb"}

        def archive(source, output):
            self.archived.append((source, output))
            Path(output).write_bytes(b"a packed target directory")
            return output

        return (
            mock.patch.object(ship.r2, "configured", return_value=self.bucket),
            mock.patch.object(ship.basis, "resolve", return_value=self.basis),
            mock.patch.object(ship.basis, "dependencies", return_value=self.depended),
            mock.patch.object(ship.build, "build", side_effect=build),
            mock.patch.object(ship.build, "archive", side_effect=archive),
            mock.patch.object(ship.build, "restore", side_effect=lambda source, packed: self.restored.append(Path(packed).read_bytes())),
        )

    def run_binary(self):
        bucket, resolve, depends, built, archive, restore = self.acting()
        self.reported = io.StringIO()
        with bucket, resolve, depends, built, archive, restore, redirect_stderr(self.reported):
            return ship.run_binary(self.held)

    def test_a_plan_that_holds_them_already_hands_them_to_the_build(self):
        self.seed()
        self.stored("skip")
        self.run_binary()
        self.assertEqual(self.restored, [b"a packed target directory"])
        self.assertEqual(self.archived, [])

    def test_a_plan_that_does_not_hold_them_yet_records_what_the_build_left(self):
        self.stored("run")
        self.run_binary()
        self.assertEqual(self.restored, [])
        self.assertEqual(len(self.archived), 1)
        self.assertIn(f"workload/1/{self.inherited}/record.json", self.bucket.objects)
        self.assertEqual(self.bucket.get(f"workload/1/{self.inherited}/blobs/{ship.DEPENDENCIES}"), b"a packed target directory")
        self.assertIn(self.inherited, self.reported.getvalue())

    def test_the_binary_is_recorded_either_way(self):
        for decision in ("run", "skip"):
            with self.subTest(decision=decision):
                self.setUp()
                self.seed()
                self.stored(decision)
                self.assertEqual(self.run_binary()["key"], self.key)

    def test_dependencies_resolving_to_another_key_stop_the_job_before_it_records_them(self):
        entries = {"binary-linux": {"key": self.key, "decision": "run"}, "dependencies-linux": {"key": "d" * 64, "decision": "run"}}
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})
        with self.assertRaisesRegex(Refusal, "the plan for this run recorded"):
            self.run_binary()
        self.assertEqual([name for name in self.bucket.objects if name.startswith("workload/1/" + "d" * 64)], [])
