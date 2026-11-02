import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from lib.process.trial import admission, context, declaration
from lib.refusal import Refusal
from tests.scripts.test_distribution import publisher
from tests.scripts.test_distribution.world import OBSERVER, producer


class Native(unittest.TestCase):
    def test_repository_endpoint_has_no_trailing_slash(self):
        with mock.patch.object(context.subprocess, "check_output", return_value='{"id":1}') as request:
            self.assertEqual(context.api(""), {"id": 1})
        self.assertEqual(request.call_args.args[0], ["gh", "api", "repos/PerishLab/wharf"])

    def inventory(self):
        return {"total_count": 1, "jobs": [{"id": 30, "run_id": 20, "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "main", "name": context.NAMES["windtun"], "status": "in_progress", "conclusion": None}]}

    def test_active_main_native_attempt_is_required(self):
        repository = {"id": 1, "full_name": "PerishLab/wharf", "default_branch": "main"}
        run = dict(publisher(), id=20, repository=repository, head_repository=repository, status="in_progress", conclusion=None)

        def api(path):
            if path == "":
                return repository
            return self.inventory() if "/jobs?" in path else run

        with mock.patch.object(context, "api", side_effect=api), mock.patch.dict(os.environ, {"GITHUB_JOB": "windtun"}):
            self.assertEqual(context.observe(json.dumps(OBSERVER), "windtun")[0], OBSERVER)
            for change in ({"head_branch": "topic"}, {"event": "pull_request"}, {"path": "other.yml"}, {"run_attempt": 2}, {"status": "completed"}, {"head_repository": dict(repository, id=2)}):
                with self.subTest(change=change), mock.patch.object(context, "api", side_effect=lambda path: repository if path == "" else dict(run, **change)), self.assertRaises(Refusal):
                    context.observe(OBSERVER, "windtun")
        with mock.patch.dict(os.environ, {"GITHUB_JOB": "other"}), self.assertRaises(Refusal):
            context.observe(OBSERVER, "windtun")

    def test_incomplete_duplicate_or_foreign_jobs_refuse(self):
        for change in ({"total_count": 2}, {"jobs": self.inventory()["jobs"] * 2, "total_count": 2}, {"jobs": [dict(self.inventory()["jobs"][0], run_attempt=2)]}):
            with self.subTest(change=change), mock.patch.object(context, "api", return_value=dict(self.inventory(), **change)), self.assertRaises(Refusal):
                context.native_jobs(OBSERVER)

    def test_missing_or_skipped_producer_steps_never_count_as_pass(self):
        held = producer()
        context.succeeded(held, [held["steps"][0]["name"]])
        for change in ({"conclusion": "cancelled"}, {"steps": []}, {"steps": held["steps"] * 2}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                context.succeeded(dict(held, **change), [held["steps"][0]["name"]])
        changed = copy.deepcopy(held)
        changed["steps"][0]["conclusion"] = "skipped"
        with self.assertRaises(Refusal):
            context.succeeded(changed, [held["steps"][0]["name"]])

    def test_attempt_fields_are_exact_and_path_safe(self):
        for change in ({"attempt": "../2"}, {"run": "0"}, {"wharf": "x" * 40}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                context.decode(dict(OBSERVER, **change))


class Publication(unittest.TestCase):
    def source(self):
        return {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1", "commit": "b" * 40, "tree": "c" * 40}

    def record(self):
        identity = self.source()
        specification = {"scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}, "cases": ["writer"], "image": "ghcr.io/perishlab/images:stable"}
        trial = {"schema": declaration.SCHEMA, "source": identity, "manifest": "d" * 64, "authority": "https://releases.plumb.perish.uk", "specification": specification}
        return dict(identity, state="complete", completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, media={"binaries": "published"}, trial=trial)

    def test_explicit_selection_pins_original_attempt_without_moving_discovery(self):
        for identity in (self.source(), None):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                selected = {name: self.source()[name] for name in ("repository", "marker")}
                held = {"source": directory, "run": "7", "publisher": json.dumps(identity), "selection": json.dumps(selected)}
                with mock.patch.object(admission, "document", return_value=self.record()), mock.patch.object(admission.subprocess, "check_output", return_value=json.dumps(dict(publisher(), display_title="not an identity"))) as provider, mock.patch.object(admission.parameters, "answer"):
                    result = admission.admit(held)
                self.assertTrue(result["eligible"])
                self.assertEqual(provider.call_count, 1)
                self.assertTrue(provider.call_args.args[0][-1].endswith("/runs/7/attempts/1"))
                self.assertEqual(json.loads((Path(directory) / "evidence/admission.json").read_text())["publisher"]["run_attempt"], 1)

    def test_legacy_discovery_is_retained_but_does_not_choose_publication_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(admission, "read", side_effect=[dict(publisher(), run_attempt=3), publisher()]) as provider, mock.patch.object(admission, "document", return_value=self.record()), mock.patch.object(admission.parameters, "answer"):
                self.assertTrue(admission.admit({"source": directory, "run": "7"})["eligible"])
            self.assertEqual(provider.call_args_list, [mock.call("7"), mock.call("7", "1", mock.ANY)])

    def test_only_identity_exact_queued_publication_may_converge(self):
        queued = dict(publisher(), status="queued", conclusion=None)
        active = dict(publisher(), status="in_progress", conclusion=None)
        with mock.patch.object(admission, "read", side_effect=[queued, active]) as provider, mock.patch.object(admission.time, "sleep") as pause:
            self.assertEqual(admission.completed("7", self.record()), active)
        self.assertEqual(provider.call_count, 2)
        pause.assert_called_once_with(10)

    def test_every_immutable_field_is_rechecked_after_wait(self):
        queued = dict(publisher(), status="queued", conclusion=None)
        changes = [{"id": 8}, {"run_attempt": 2}, {"head_sha": "f" * 40}, {"repository": {"full_name": "SECRET"}}, {"head_repository": None}, {"event": "push"}, {"path": "other.yml"}, {"head_branch": "topic"}]
        for change in changes:
            with self.subTest(change=change), mock.patch.object(admission, "read", side_effect=[queued, dict(publisher(), **change)]) as provider, mock.patch.object(admission.time, "sleep") as pause:
                with self.assertRaises(Refusal) as failure:
                    admission.completed("7", self.record())
                self.assertIn("Windtun publisher run 7 attempt", str(failure.exception))
                self.assertNotIn("SECRET", str(failure.exception))
                self.assertEqual(provider.call_count, 2)
                pause.assert_called_once_with(10)

    def test_exhaustion_never_returns_trusted_metadata(self):
        with mock.patch.object(admission, "read", return_value=dict(publisher(), status="queued", conclusion=None)) as provider, mock.patch.object(admission.time, "sleep") as pause, mock.patch.object(admission.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(Refusal, "exhausted 18"):
                admission.completed("7", self.record())
        self.assertEqual((provider.call_count, pause.call_count), (19, 18))

    def test_elapsed_budget_bounds_reads_and_waits(self):
        with mock.patch.object(admission, "read", return_value=dict(publisher(), status="queued", conclusion=None)) as provider, mock.patch.object(admission.time, "sleep") as pause, mock.patch.object(admission.time, "monotonic", side_effect=[0, 179, 179, 179.5, 180]):
            with self.assertRaisesRegex(Refusal, "exceeded 180"):
                admission.completed("7", self.record())
        provider.assert_called_once_with("7", "1", 1)
        pause.assert_called_once_with(0.5)

    def test_late_active_response_cannot_escape_elapsed_budget(self):
        with mock.patch.object(admission, "read", return_value=publisher()) as provider, mock.patch.object(admission.time, "monotonic", side_effect=[0, 0, 181]), self.assertRaisesRegex(Refusal, "observation exceeded 180"):
            admission.completed("7", self.record())
        provider.assert_called_once()

    def test_unknown_states_and_inconsistent_queue_never_wait(self):
        for change in [{"status": state} for state in (None, "waiting", "pending", "requested", "SECRET")] + [{"status": "queued", "conclusion": "cancelled"}]:
            with self.subTest(change=change), mock.patch.object(admission, "read", return_value=dict(publisher(), **change)) as provider, mock.patch.object(admission.time, "sleep") as pause:
                with self.assertRaises(Refusal) as failure:
                    admission.completed("7", self.record())
                self.assertNotIn("SECRET", str(failure.exception))
                provider.assert_called_once()
                pause.assert_not_called()

    def test_provider_errors_and_malformed_metadata_are_safe_and_not_retried(self):
        failures = [OSError("SECRET"), subprocess.CalledProcessError(1, ["gh"], stderr="SECRET"), subprocess.TimeoutExpired(["gh"], 30)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch.object(admission.subprocess, "check_output", side_effect=failure) as provider, mock.patch.object(admission.time, "sleep") as pause:
                with self.assertRaisesRegex(Refusal, "run 7 attempt 1: native metadata read") as caught:
                    admission.completed("7", self.record())
                self.assertNotIn("SECRET", str(caught.exception))
                provider.assert_called_once()
                self.assertEqual(provider.call_args.kwargs["stderr"], subprocess.PIPE)
                pause.assert_not_called()
        for raw in ("SECRET", "[]", "null"):
            with self.subTest(raw=raw), mock.patch.object(admission.subprocess, "check_output", return_value=raw), self.assertRaises(Refusal):
                admission.read("7", "1")

    def test_foreign_or_malformed_record_refuses_before_native_read(self):
        mutations = [{"run": "8"}, {"attempt": "../2"}, {"wharf": "SECRET"}]
        for change in mutations:
            record = dict(self.record(), completed=dict(self.record()["completed"], **change))
            with self.subTest(change=change), mock.patch.object(admission, "read") as provider, self.assertRaises(Refusal):
                admission.completed("7", record)
            provider.assert_not_called()

    def test_incomplete_or_source_swapped_publication_never_reads_native_metadata(self):
        for change in ({"state": "incomplete"}, {"commit": "f" * 40}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(admission, "document", return_value=dict(self.record(), **change)), mock.patch.object(admission, "read") as provider, self.assertRaises(Refusal):
                    admission.admit({"source": directory, "run": "7", "publisher": json.dumps(self.source())})
                provider.assert_not_called()
                self.assertFalse((Path(directory) / "evidence/admission.json").exists())

    def test_selected_product_mismatch_refuses_without_any_provider_read(self):
        held = {"run": "7", "publisher": json.dumps(self.source()), "selection": json.dumps({"repository": "PerishLab/keel", "marker": self.source()["marker"]})}
        with mock.patch.object(admission, "read") as provider, self.assertRaises(Refusal):
            admission.chosen(held)
        provider.assert_not_called()
