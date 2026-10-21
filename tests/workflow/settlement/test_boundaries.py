import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.process.lane import execution, settlement
from lib.refusal import Refusal
from lib.store.lane import documents
from tests.workflow.settlement import fixtures


class Boundaries(unittest.TestCase):
    def invoke(self, held):
        with fixtures.runtime(held):
            return settlement.perform(held["held"], held["env"])

    def assert_reserved(self, held):
        self.assertEqual(held["store"].current, held["value"]["current"])
        self.assertFalse(any(call[0] in ("swap", "create") for call in held["store"].calls))

    def test_each_native_failure_cancel_unknown_or_running_job_refuses_before_write(self):
        for selected in range(8):
            for changed in ({"status": "in_progress", "conclusion": None}, {"conclusion": "failure"},
                            {"conclusion": "cancelled"}, {"conclusion": None}, {"conclusion": "timed_out"}):
                if selected == 0 and changed == {"status": "in_progress", "conclusion": None}:
                    continue
                held = fixtures.bundle()
                held["jobs"]["jobs"][selected].update(changed)
                with self.subTest(job=selected, changed=changed), self.assertRaises(Refusal):
                    self.invoke(held)
                self.assert_reserved(held)
                self.assertNotIn("reads", held)

    def test_missing_duplicate_truncated_foreign_or_unexpected_native_jobs_refuse(self):
        for failure in ("missing", "duplicate", "id", "truncated", "oversized", "foreign-run", "attempt", "head", "name", "state", "malformed"):
            held = fixtures.bundle()
            jobs = held["jobs"]["jobs"]
            if failure == "missing":
                jobs.pop(0)
                held["jobs"]["total_count"] -= 1
            elif failure == "duplicate":
                jobs[1]["name"] = jobs[0]["name"]
            elif failure == "id":
                jobs[1]["id"] = jobs[0]["id"]
            elif failure == "truncated":
                held["jobs"]["total_count"] += 1
            elif failure == "oversized":
                held["jobs"]["jobs"] = jobs * 4
                held["jobs"]["total_count"] = len(jobs) * 4
            elif failure == "state":
                jobs[-1]["status"] = "completed"
            elif failure == "malformed":
                held["jobs"]["total_count"] = True
            else:
                field = {"foreign-run": "run_id", "attempt": "run_attempt", "head": "head_sha", "name": "name"}[failure]
                jobs[0][field] = {"foreign-run": 456, "attempt": 2, "head": "9" * 40, "name": "untrusted"}[failure]
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.invoke(held)
            self.assert_reserved(held)

    def test_native_provider_unavailability_never_infers_quiescence(self):
        held = fixtures.bundle()
        held["api"].jobs.side_effect = Refusal("unknown jobs")
        with self.assertRaises(Refusal):
            self.invoke(held)
        self.assert_reserved(held)

    def test_skipped_provider_call_names_are_one_branch_not_a_second_writer(self):
        for action in ("apply", "dispose", "inspect"):
            held = fixtures.bundle(action)
            for job in held["jobs"]["jobs"]:
                if job["conclusion"] == "skipped" and job["name"].startswith("[deploy]"):
                    job["name"] = job["name"].split(" / ")[0]
            self.assertIn(self.invoke(held)["phase"], ("settled", "observed"))
        held = fixtures.bundle()
        repeated = copy.deepcopy(held["jobs"]["jobs"][6])
        repeated.update(id=100, name="[deploy] cfworker disposal")
        held["jobs"]["jobs"].append(repeated)
        held["jobs"]["total_count"] += 1
        with self.assertRaises(Refusal):
            self.invoke(held)
        self.assert_reserved(held)

    def test_attempt_endpoint_binds_omitted_attempt_but_boolean_attempt_refuses(self):
        held = fixtures.bundle()
        for job in held["jobs"]["jobs"]:
            del job["run_attempt"]
        self.assertEqual(self.invoke(held)["phase"], "settled")
        held = fixtures.bundle()
        held["jobs"]["jobs"][0]["run_attempt"] = True
        with self.assertRaises(Refusal):
            self.invoke(held)
        self.assert_reserved(held)

    def test_actual_quiescence_reader_selects_original_run_attempt_not_outputs(self):
        held = fixtures.bundle()
        env = dict(held["env"], WHARF_PREVIEW_CONTROL_TOKEN="read-only")
        with mock.patch.object(execution, "GitHub", return_value=held["api"]):
            result = execution.quiescence(held["intent"], env)
        self.assertEqual(result, held["jobs"])
        held["api"].jobs.assert_called_once_with(123, 1)
        self.assertFalse(any(call[0] in ("create", "swap") for call in held["store"].calls))

    def test_registration_current_custody_and_permission_movement_at_write_edges_refuse(self):
        for edge in ("before", "public", "history"):
            for failure in ("registration", "current", "custody", "permission", "cancelled", "writer"):
                held = fixtures.bundle()
                before = copy.deepcopy(held["store"].current)

                def move():
                    if failure == "registration":
                        held["store"].target["enabled"] = False
                    elif failure == "current":
                        held["store"].current["phase"] = "unknown"
                    elif failure == "custody":
                        held["store"].objects[execution.location(held["intent"])] = b"{}"
                    elif failure == "permission":
                        held["api"].permissions["alice"]["permission"] = "read"
                    elif failure == "cancelled":
                        held["api"].observed["status"] = "completed"
                    else:
                        held["jobs"]["jobs"][5]["status"] = "in_progress"

                if edge == "before":
                    move()
                elif edge == "public":
                    from tests.workflow.publication import fixtures as published
                    original = published.Connection.getresponse

                    def response(connection):
                        result = original(connection)
                        move()
                        return result

                    held["response_hook"] = mock.patch.object(published.Connection, "getresponse", response)
                else:
                    original = held["store"].create

                    def create(key, body):
                        original(key, body)
                        move()

                    held["store"].create = create
                with self.subTest(edge=edge, failure=failure), fixtures.runtime(held), held.get("response_hook", mock.patch.object(held["api"], "source", held["api"].source)), self.assertRaises(Refusal):
                    settlement.perform(held["held"], held["env"])
                self.assertFalse(any(call[0] == "swap" for call in held["store"].calls))
                if failure != "current":
                    self.assertEqual(held["store"].current, before)

    def test_forbidden_writers_direct_and_file_backed_refuse_before_read(self):
        names = ("WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_LANE_EVIDENCE_ACCESS_KEY_ID",
                 "WHARF_LANE_EVIDENCE_SECRET_ACCESS_KEY", "WHARF_R2_ACCESS_KEY_ID", "WHARF_LANE_PROVIDER_TOKEN",
                 "WHARF_LANE_WORKLOAD_READER_ACCESS_KEY_ID", "WHARF_LANE_PROVIDER_READER_TOKEN", "CLOUDFLARE_API_TOKEN")
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "credential"
            secret.write_text("extra-role")
            for name in names:
                for key, value in ((name, "extra-role"), (name + "_FILE", str(secret))):
                    held = fixtures.bundle()
                    held["env"][key] = value
                    with self.subTest(key=key), self.assertRaises(Refusal):
                        self.invoke(held)
                    self.assert_reserved(held)
                    held["api"].jobs.assert_not_called()

    def test_unknown_continuation_and_replayed_run_attempt_never_write(self):
        for failure in ("unknown", "attempt", "run"):
            held = fixtures.bundle()
            if failure == "unknown":
                held["store"].current["phase"] = "unknown"
            else:
                held["env"]["GITHUB_RUN_ATTEMPT" if failure == "attempt" else "GITHUB_RUN_ID"] = "2" if failure == "attempt" else "456"
            with self.assertRaises(Refusal):
                self.invoke(held)
            self.assertFalse(any(call[0] in ("create", "swap") for call in held["store"].calls))


if __name__ == "__main__":
    unittest.main()
