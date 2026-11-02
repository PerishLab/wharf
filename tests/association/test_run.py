import os
import subprocess
import unittest
from unittest import mock

from lib.identity.bind import control, storage
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
