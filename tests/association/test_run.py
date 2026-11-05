import copy
import json
import os
import subprocess
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind import comparison, control, storage
from lib.identity.bind import producer as legacy
from lib.store import distribution, plan, workload
from tests.scripts.test_distribution.binary_world import AUTHORITY
from lib.process.association import control as native
from lib.process.association import producer, public
from lib.refusal import Refusal
from tests.association.world import World

IDENTITY = {"run": "7", "attempt": "1", "wharf": "a" * 40}
REPOSITORY = {"id": 1374358737, "node_id": "R_kgDOUesM0Q", "full_name": "PerishLab/wharf", "default_branch": "main"}


def run(state="in_progress"):
    return {"id": 7, "run_attempt": 1, "head_sha": "a" * 40, "repository": REPOSITORY, "head_repository": REPOSITORY, "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "head_branch": "main", "status": state, "conclusion": "success" if state == "completed" else None}


class Trusted(unittest.TestCase):
    def test_exact_queued_reads_converge_without_accepting_queued_as_trusted(self):
        for state in ("in_progress", "completed"):
            final = run(state)
            with self.subTest(state=state), mock.patch.object(control, "api", side_effect=[run("queued"), final]) as api, mock.patch.object(control.time, "sleep") as sleep:
                self.assertEqual(control.trusted(IDENTITY), final)
                self.assertEqual(api.call_count, 2)
                sleep.assert_called_once_with(10)

    def test_exhaustion_retains_exact_observer_and_attempt_diagnostic(self):
        with mock.patch.object(control, "api", return_value=run("queued")) as api, mock.patch.object(control.time, "sleep") as sleep:
            with self.assertRaisesRegex(Refusal, "observer run 7 attempt 1: native status queued did not converge"):
                control.trusted(IDENTITY, "observer")
            self.assertEqual(api.call_count, 19)
            self.assertEqual(sleep.call_count, 18)

    def test_each_identity_field_refuses_before_any_queued_wait(self):
        changes = [("id", 8, "run id"), ("run_attempt", 2, "attempt"), ("head_sha", "b" * 40, "source"), ("head_repository", {"full_name": "Other/wharf"}, "repository"), ("repository", {"full_name": "Other/wharf"}, "repository"), ("path", ".github/workflows/other.yml", "workflow path"), ("event", "push", "event"), ("head_branch", "feature/319", "branch")]
        for field, value, reason in changes:
            with self.subTest(field=field), mock.patch.object(control, "api", return_value=run("queued") | {field: value}) as api, mock.patch.object(control.time, "sleep") as sleep:
                with self.assertRaisesRegex(Refusal, "publisher run 7 attempt 1: native " + reason + " differs"):
                    control.trusted(IDENTITY, "publisher")
                api.assert_called_once()
                sleep.assert_not_called()

    def test_identity_movement_during_queued_wait_stops_without_another_pause(self):
        with mock.patch.object(control, "api", side_effect=[run("queued"), run("queued") | {"head_sha": "b" * 40}]) as api, mock.patch.object(control.time, "sleep") as sleep:
            with self.assertRaisesRegex(Refusal, "source differs"):
                control.trusted(IDENTITY)
            self.assertEqual(api.call_count, 2)
            sleep.assert_called_once_with(10)

    def test_unknown_and_other_known_states_refuse_without_printing_provider_prose(self):
        for state in ("waiting", "requested", "pending", "private provider prose"):
            with self.subTest(state=state), mock.patch.object(control, "api", return_value=run(state)), mock.patch.object(control.time, "sleep") as sleep:
                with self.assertRaises(Refusal) as raised:
                    control.trusted(IDENTITY)
                self.assertNotIn("private provider prose", str(raised.exception))
                self.assertIn("native status " + (state if state != "private provider prose" else "unknown"), str(raised.exception))
                sleep.assert_not_called()

    def test_queued_with_conclusion_is_incoherent_and_never_waits(self):
        with mock.patch.object(control, "api", return_value=run("queued") | {"conclusion": "failure"}), mock.patch.object(control.time, "sleep") as sleep, self.assertRaises(Refusal):
            control.trusted(IDENTITY)
        sleep.assert_not_called()

    def test_provider_failure_after_queued_is_not_retried(self):
        failure = subprocess.CalledProcessError(1, "gh")
        with mock.patch.object(control, "api", side_effect=[run("queued"), failure]) as api, mock.patch.object(control.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                control.trusted(IDENTITY)
            self.assertEqual(api.call_count, 2)
            sleep.assert_called_once_with(10)

    def test_unknown_internal_purpose_cannot_change_admission(self):
        with mock.patch.object(control, "api") as api, self.assertRaises(Refusal):
            control.trusted(IDENTITY, "permission")
        api.assert_not_called()

    def test_completed_historical_metadata_never_admits_an_active_role(self):
        with mock.patch.dict(os.environ, {"GITHUB_JOB": "build-proof"}), mock.patch.object(control, "api", side_effect=[run("completed"), REPOSITORY]) as api:
            with self.assertRaisesRegex(Refusal, "observer is not active"):
                control.active(IDENTITY)
            self.assertEqual(api.call_count, 2)

    def test_original_producer_plan_is_read_only_after_trusted_convergence(self):
        context = {"repository": "PerishLab/demo", "marker": "v1.2.3", **IDENTITY}
        loaded = {"key": "b" * 64, "record": {"context": context}}
        document = {"context": context, "entries": {"bind-linux": {"key": loaded["key"], "decision": "run"}}}
        with mock.patch.object(control, "api", side_effect=[run("queued"), run("completed")]), mock.patch.object(control.time, "sleep"), mock.patch.object(storage.plan, "read", return_value=document) as read:
            storage.producer(object(), loaded, "bind-linux")
            read.assert_called_once()
        with mock.patch.object(control, "api", return_value=run("queued") | {"event": "push"}), mock.patch.object(storage.plan, "read") as read, self.assertRaisesRegex(Refusal, "producer run 7 attempt 1: native event differs"):
            storage.producer(object(), loaded, "bind-linux")
        read.assert_not_called()


class Composition(unittest.TestCase):
    def test_native_role_still_checks_its_actual_job_after_queued_convergence(self):
        world = World()
        calls = []
        endpoint = f"actions/runs/{world.observer['run']}/attempts/1"

        def api(path):
            value = world.api(path)
            if path == endpoint:
                calls.append(path)
                if len(calls) == 1:
                    value["status"] = "queued"
            return value

        with world.wired(), mock.patch.object(control, "api", side_effect=api), mock.patch.object(control.time, "sleep") as sleep:
            self.assertEqual(native.active(world.observer, "native-proof")[0], world.observer)
            sleep.assert_called_once_with(10)
        self.assertEqual(len(calls), 2)

    def test_public_reader_distinguishes_proof_issuer_and_product_publisher(self):
        world = World()
        with world.wired():
            producer.produce(world.held)
            with mock.patch.object(control, "trusted", wraps=control.trusted) as trusted:
                public.read("PerishLab/demo", world.source["marker"], "aarch64-apple-darwin")
            trusted.assert_any_call(world.observer, "issuer")
            trusted.assert_any_call(world.publisher, "publisher")


def retained(world):
    reference = plan.reference(world.plan)
    for entry in world.plan["entries"].values():
        path = workload.prefix(entry["key"]) + "record.json"
        record = json.loads(world.blob.get(path))
        record["context"] = plan.recorded(dict(record["context"], attempt="2"), world.plan)
        world.blob.objects[path] = canonical.encode(record)
    world.publisher["attempt"] = "2"
    world.distribution["plan"] = reference
    world.documents[AUTHORITY + "/" + world.root + "/distribution.json"] = canonical.encode(world.distribution)
    return reference


def api(world, path):
    value = world.api(path)
    if "/attempts/" not in path:
        return value
    attempt = int(path.split("/attempts/")[1].split("/")[0])
    if "/jobs?" not in path:
        return dict(value, run_attempt=attempt)
    run = path.split("/")[2]
    if run == world.observer["run"]:
        return value
    job = dict(value["jobs"][0], name="[plan] entries", run_attempt=attempt, status="completed", conclusion="success", steps=[{"name": "[record] plan", "status": "completed", "conclusion": "success"}])
    return {"total_count": 1, "jobs": [job]}


class Retained(unittest.TestCase):
    def test_legacy_linux_public_read_adds_no_native_api_dependency(self):
        world = World()
        world.role = "build-proof"
        with world.wired():
            legacy.produce(world.held)
            with mock.patch.object(control, "api", side_effect=AssertionError("legacy read acquired a new API dependency")):
                proof, _ = comparison.read(world.source["repository"], world.source["marker"])
            self.assertNotIn("plan", proof)

    def test_rerun_proves_and_independently_reads_linux_and_native_content(self):
        world = World()
        reference = retained(world)
        with world.wired(), mock.patch.object(control, "api", side_effect=lambda path: api(world, path)):
            producer.produce(world.held)
            self.assertEqual(len(public.check(world.held)["targets"]), 2)
            for selected in world.native:
                proof, _ = public.read(world.source["repository"], world.source["marker"], selected)
                self.assertEqual(proof["plan"], reference)
                self.assertEqual(proof["publisher"]["attempt"], "2")
                self.assertEqual(proof["build"]["producer"]["plan"]["attempt"], "1")
            world.role = "build-proof"
            with mock.patch.dict(os.environ, {"GITHUB_JOB": world.role}):
                legacy.produce(world.held)
                proof, _ = comparison.read(world.source["repository"], world.source["marker"])
            self.assertEqual(proof["plan"], reference)
            self.assertEqual(proof["binding"]["producer"]["attempt"], "2")
        self.assertNotIn(plan.location(dict(world.plan["context"], attempt="2")), world.blob.objects)

    def test_fresh_observation_preserves_first_completion_and_original_producer(self):
        world = World()
        reference = retained(world)
        fresh_plan = copy.deepcopy(world.plan)
        fresh_plan["context"].update(run="9", attempt="1")
        for entry in fresh_plan["entries"].values():
            entry["decision"] = "skip"
        world.blob.objects[plan.location(fresh_plan["context"])] = canonical.encode(fresh_plan)
        current = {"run": "9", "attempt": "1", "wharf": world.publisher["wharf"]}
        fresh = dict(world.distribution, attempt=current, completed=current, plan=plan.reference(fresh_plan))
        world.distribution = distribution.merged(world.distribution, fresh)
        self.assertEqual(world.distribution["completed"], world.publisher)
        self.assertEqual(world.distribution["plan"], reference)
        world.documents[AUTHORITY + "/" + world.root + "/distribution.json"] = canonical.encode(world.distribution)
        with world.wired(), mock.patch.object(control, "api", side_effect=lambda path: api(world, path)):
            producer.produce(world.held)
            self.assertEqual(len(public.check(world.held)["targets"]), 2)

    def test_missing_legacy_producer_plan_never_searches_an_earlier_attempt(self):
        world = World()
        context = dict(world.build_context, attempt="2")
        loaded = {"key": world.build_key, "record": {"context": context}}
        with world.wired(), mock.patch.object(control, "api", side_effect=lambda path: api(world, path)), mock.patch.object(world.blob, "get", wraps=world.blob.get) as get:
            with self.assertRaises(Refusal):
                storage.producer(world.blob, loaded, "binary-linux")
            get.assert_called_once_with(plan.location(context))

    def test_foreign_future_tampered_and_missing_plan_references_refuse(self):
        world = World()
        reference = retained(world)
        context = dict(world.build_context, attempt="2")
        for changes in ({"run": "9"}, {"repository": "Other/demo"}, {"wharf": "f" * 40}, {"attempt": "3"}, {"sha256": "f" * 64}, {"commit": "f" * 40}, {"attempt": "02"}, {"extra": True}, {"marker": None}):
            with self.subTest(changes=changes), self.assertRaises(Refusal):
                plan.referenced(world.blob, context, dict(reference, **changes))
        location = plan.location(world.plan["context"])
        del world.blob.objects[location]
        with self.assertRaises(Refusal):
            plan.referenced(world.blob, context, reference)

    def test_plan_digest_does_not_replace_entry_or_native_planner_checks(self):
        world = World()
        reference = retained(world)
        context = dict(world.build_context, attempt="2", plan=reference)
        loaded = {"key": "e" * 64, "record": {"context": context}}
        with world.wired(), mock.patch.object(control, "api", side_effect=lambda path: api(world, path)):
            with self.assertRaisesRegex(Refusal, "differs from its producer"):
                storage.producer(world.blob, loaded, "binary-linux")
            with mock.patch.object(storage.publication, "jobs", return_value=[{"name": "[plan] entries", "status": "completed", "conclusion": "failure"}]), self.assertRaisesRegex(Refusal, "planner did not succeed"):
                storage.origin(context)

    def test_public_readback_rejects_a_retargeted_distribution_plan(self):
        world = World()
        retained(world)
        with world.wired(), mock.patch.object(control, "api", side_effect=lambda path: api(world, path)):
            producer.produce(world.held)
            world.distribution["plan"]["sha256"] = "e" * 64
            world.documents[AUTHORITY + "/" + world.root + "/distribution.json"] = canonical.encode(world.distribution)
            with self.assertRaisesRegex(Refusal, "plan reference differs"):
                public.check(world.held)
