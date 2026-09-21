import io
import json
import os
import tempfile
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
        self.held = dict(CONTEXT, planned="1", source="../product", target="x86_64-unknown-linux-gnu")

    def stored(self, entries):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})

    def built(self, produced):
        def build(request):
            if Path(request.output).exists():
                raise Refusal(f"output {request.output} already exists")
            Path(request.output).mkdir(parents=True)
            Path(request.output).joinpath("plumb").write_bytes(produced)
            return {"artifact": "plumb"}

        return mock.patch.object(ship.build, "build", side_effect=build)

    def running(self):
        return mock.patch.object(ship.r2, "configured", return_value=self.bucket), mock.patch.object(ship.basis, "resolve", return_value=self.basis)

    def run_binary(self):
        bucket, resolve = self.running()
        with bucket, resolve, self.built(b"an executable"):
            return ship.run_binary(self.held)

    def test_the_action_is_handed_a_place_that_does_not_exist_yet(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "run"}})
        self.run_binary()
        self.assertFalse(ship.place().exists())

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

    def test_a_failed_job_run_again_reads_the_plan_its_run_recorded(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "run"}})
        self.held.update(attempt="2", planned="1")
        self.assertEqual(self.run_binary()["state"], "recorded")
        record = json.loads(self.bucket.objects[f"workload/1/{self.key}/record.json"])
        self.assertEqual(record["context"], dict(CONTEXT, attempt="2"))

    def test_a_plan_that_decided_to_skip_stops_the_job(self):
        self.stored({"binary-linux": {"key": self.key, "decision": "skip"}})
        with self.assertRaisesRegex(Refusal, "decided 'skip' for binary-linux"):
            self.run_binary()

    def test_a_plan_holding_another_key_stops_the_job_before_it_records_anything(self):
        self.stored({"binary-linux": {"key": "d" * 64, "decision": "run"}})
        with self.assertRaisesRegex(Refusal, "the plan for this run recorded"):
            self.run_binary()
        self.assertEqual([name for name in self.bucket.objects if name.startswith("workload/")], [])


class Binding(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.held = dict(CONTEXT, planned="1")
        self.identity = {"repository": CONTEXT["repository"], "marker": CONTEXT["marker"], "commit": "a" * 40, "tree": "b" * 40}
        self.witnessed = []
        self.binaries = {"linux": "e" * 64, "windows": "f" * 64}
        self.keys = {name: self.keyed(key) for name, key in self.binaries.items()}

    def keyed(self, binary):
        basis = {"entry": {"kind": "binary-identity", "binary": binary}, "identity": self.identity}
        return ship.workload.key(basis, ship.implementation.resourced(["lib.identity.bind"], ["identity/format.json"]))

    def seed(self, decisions):
        entries = {}
        for name, decision in decisions.items():
            produced = Path(tempfile.mkdtemp()) / "unbound"
            produced.mkdir()
            produced.joinpath(f"plumb-{ship.targeted(name)['target']}").write_bytes(b"an unbound executable")
            ship.workload.publish(self.bucket, self.binaries[name], ship.workload.Produced(produced, {}, {}))
            entries[f"binary-{name}"] = {"key": self.binaries[name], "decision": "skip"}
            entries[f"bind-{name}"] = {"key": self.keys[name], "decision": decision}
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})

    def bound(self):
        def perform(artifact, release, workload, output):
            self.witnessed.append((artifact.directory, release, workload))
            Path(output).mkdir(parents=True)
            Path(output).joinpath(artifact.file.name).write_bytes(b"a bound executable")
            return {"binding": release.marker}

        return mock.patch.object(ship.bind, "perform", side_effect=perform)

    def run_bind(self):
        with mock.patch.object(ship.r2, "configured", return_value=self.bucket), self.bound():
            return ship.run_bind(self.held)

    def test_the_binary_it_binds_is_the_one_the_plan_named(self):
        self.seed({"linux": "run"})
        self.assertEqual([record["key"] for record in self.run_bind()], [self.keys["linux"]])
        self.assertEqual(self.witnessed[0][2], self.binaries["linux"])
        self.assertTrue(self.witnessed[0][0].joinpath("plumb-x86_64-unknown-linux-gnu").is_file())

    def test_the_identity_it_binds_comes_from_the_plan_not_from_the_job(self):
        self.seed({"linux": "run"})
        self.run_bind()
        self.assertEqual(self.witnessed[0][1], ship.bind.Release(**self.identity))

    def test_one_job_binds_every_target_the_plan_decided_to_run(self):
        self.seed({"linux": "run", "windows": "run"})
        self.assertEqual([record["key"] for record in self.run_bind()], [self.keys["linux"], self.keys["windows"]])
        self.assertEqual([witnessed[2] for witnessed in self.witnessed], [self.binaries["linux"], self.binaries["windows"]])

    def test_a_target_the_plan_decided_to_skip_is_absent_from_the_job(self):
        self.seed({"linux": "run", "windows": "skip"})
        self.assertEqual([record["key"] for record in self.run_bind()], [self.keys["linux"]])

    def test_a_plan_that_decided_to_skip_every_target_stops_the_job(self):
        self.seed({"linux": "skip", "windows": "skip"})
        with self.assertRaisesRegex(Refusal, "no target needs binding"):
            self.run_bind()


class Recording(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.held = dict(CONTEXT, planned="1", source="../product")

    def keyed(self, held, modules):
        return ship.workload.key(held, ship.implementation.resourced(*modules))

    def seed(self, name, key):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), {name: {"key": key, "decision": "run"}}, {})

    def acted(self, module, action):
        def act(request):
            Path(request.output).mkdir(parents=True)
            Path(request.output).joinpath("receipt.json").write_text("{}")
            return {"action": action}

        return mock.patch.object(module, action, side_effect=act)

    def record(self, handler, acting, resolving):
        with mock.patch.object(ship.r2, "configured", return_value=self.bucket), self.acted(*acting), resolving:
            return handler(self.held)

    def test_the_workspace_suite_is_recorded_under_the_key_the_plan_holds(self):
        held_basis = {"entry": {"kind": "cargo-suite"}}
        key = self.keyed(held_basis, (["lib.cargo.basis", "lib.cargo.suite"], []))
        self.seed("suite-linux", key)
        resolving = mock.patch.object(ship.basis, "suite", return_value=held_basis)
        self.assertEqual(self.record(ship.run_suite, (ship.suite, "suite"), resolving)["key"], key)

    def test_the_node_suite_is_recorded_under_the_key_the_plan_holds(self):
        held_basis = {"entry": {"kind": "node-suite"}}
        key = self.keyed(held_basis, (["lib.media.node"], []))
        self.seed("suite-node", key)
        resolving = mock.patch.object(ship.node, "basis", return_value=held_basis)
        self.assertEqual(self.record(ship.node_suite, (ship.node, "suite"), resolving)["key"], key)

    def test_the_workers_are_recorded_under_the_key_the_plan_holds(self):
        held_basis = {"entry": {"kind": "cfworker"}}
        key = self.keyed(held_basis, (["lib.media.cfworker"], []))
        self.seed("cfworker", key)
        resolving = mock.patch.object(ship.cfworker, "basis", return_value=held_basis)
        self.assertEqual(self.record(ship.cfworker_deploy, (ship.cfworker, "deploy"), resolving)["key"], key)

    def test_a_suite_the_plan_decided_to_skip_stops_the_job(self):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), {"suite-linux": {"key": "d" * 64, "decision": "skip"}}, {})
        resolving = mock.patch.object(ship.basis, "suite", return_value={})
        with self.assertRaisesRegex(Refusal, "decided 'skip' for suite-linux"):
            self.record(ship.run_suite, (ship.suite, "suite"), resolving)


class Ship(unittest.TestCase):
    def test_refusal_exits_two(self):
        argv = ["smoke", "--target", "x86_64-unknown-linux-gnu", "--repository", "PerishLab/plumb", "--marker", "v0.38.3", "--wharf", "c" * 40, "--run", "1", "--attempt", "1"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            code = ship.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("refused", error.getvalue())

    def test_the_key_a_plan_holds_is_the_one_a_job_resolves(self):
        held = {"entry": {"kind": "binary-smoke", "binary": "a" * 64}}
        modules = (["lib.identity.smoke"], [])
        first = ship.workload.key(held, ship.implementation.resourced(*modules))
        self.assertEqual(ship.resolved({"key": first, "decision": "run"}, held, modules), first)


class Stepped(unittest.TestCase):
    def test_a_layer_unit_runs_the_action_it_names_with_that_action_s_own_parameters(self):
        seen = {}
        handler = lambda held: seen.update(held) or {"state": "smoked"}
        environment = {"WHARF_TARGET": "x86_64-unknown-linux-gnu", **{f"WHARF_{name.upper()}": value for name, value in dict(CONTEXT, planned="1").items()}}
        with mock.patch.dict(ship.ACTIONS, {"smoke": (handler, ship.ACTIONS["smoke"][1])}), mock.patch.dict(os.environ, environment), redirect_stderr(io.StringIO()):
            self.assertEqual(ship.step({"unit": "smoke"}), {"state": "smoked"})
        self.assertEqual(seen["target"], "x86_64-unknown-linux-gnu")
        self.assertEqual(seen["planned"], "1")

    def test_a_unit_no_layer_runs_refuses(self):
        with self.assertRaisesRegex(Refusal, r"a layer runs one of (\w|-|, )+, not cargo-publish"):
            ship.step({"unit": "cargo-publish"})


class Validating(unittest.TestCase):
    def test_validation_is_recorded_under_its_own_key_from_the_primary_bound_binary(self):
        bucket = Memory()
        produced = Path(tempfile.mkdtemp()) / "bound"
        produced.mkdir()
        (produced / "plumb-x86_64-unknown-linux-gnu").write_bytes(b"a bound executable")
        bound = "b" * 64
        ship.workload.publish(bucket, bound, ship.workload.Produced(produced, {"entry": "bound"}, CONTEXT))
        held_basis = {"entry": {"kind": "binary-validate", "binary": bound}}
        key = ship.workload.key(held_basis, ship.implementation.resourced(["lib.identity.smoke"], ["validators.json"]))
        entries = {"bind-linux": {"key": bound, "decision": "run"}, "validate": {"key": key, "decision": "run"}}
        plan.record(bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})
        seen = {}

        def configure(artifact, output, release):
            seen.update(file=artifact.file.read_bytes(), release=release)
            Path(output).mkdir(parents=True)
            Path(output).joinpath("receipt.json").write_text("{}")

        with mock.patch.object(ship.r2, "configured", return_value=bucket), mock.patch.object(ship, "configured", side_effect=configure):
            self.assertEqual(ship.validate(dict(CONTEXT, planned="1"))["key"], key)
        self.assertEqual(seen["file"], b"a bound executable")
        self.assertEqual(seen["release"], {"repository": CONTEXT["repository"], "marker": CONTEXT["marker"]})
