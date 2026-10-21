import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.confirmation import original
from lib.refusal import Refusal
from tests.confirmation.fixtures import bundle


class Original(unittest.TestCase):
    def test_terminal_failure_is_exact_read_only_provenance_without_settlement(self):
        for action in ("apply", "dispose"):
            intent, retained, api, inventory = bundle(action)
            result = original.observe(intent, retained, api)
            self.assertEqual(result["request"], codec.digest("request", retained["request"]))
            self.assertEqual(result["workflow"]["conclusion"], "failure")
            self.assertEqual(result["custody"], retained["custody"])
            self.assertEqual(len(result["jobs"]), inventory["total_count"])
            self.assertEqual(api.calls, [("repository", "PerishLab/wharf"), ("run", 123), ("jobs", 123, 1), ("run", 123)])
            self.assertEqual(set(result), {"schema", "request", "custody", "workflow", "jobs"})
            self.assertNotIn("writer", result)
            result["custody"]["attempt"] = 2
            result["workflow"]["actors"]["actor"]["login"] = "observer"
            self.assertEqual(retained["custody"]["attempt"], 1)
            self.assertEqual(api.observed["actor"]["login"], "alice")

    def test_missing_foreign_or_malformed_retained_basis_refuses_before_io(self):
        changes = [("custody", "schema", "wharf.lane.custody/v2"), ("custody", "request", "0" * 64),
                   ("custody", "attempt", True), ("custody", "run", 0), ("custody", "run", 2 ** 64),
                   ("custody", "commit", "main"), ("actors", "actor", {"id": True, "login": "alice"}),
                   ("actors", "actor", {"id": 20, "login": "../alice"}), ("actors", "observer", {})]
        for section, key, value in changes:
            intent, retained, api, _ = bundle()
            retained[section][key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            self.assertFalse(api.calls)
        intent, retained, api, _ = bundle()
        del retained["actors"]
        with self.assertRaises(Refusal):
            original.observe(intent, retained, api)
        self.assertFalse(api.calls)

    def test_exact_source_request_caller_and_control_binding_precedes_reads(self):
        for field, value in (("caller", "observer"), ("workflow", "PerishLab/wharf/.github/workflows/ship.yml@" + "1" * 40),
                             ("request", "0" * 64), ("target", {"repository": "PerishLab/crest", "app": "crest-review", "lane": "design"}),
                             ("source", {"commit": "2" * 40, "tree": "e" * 40, "declaration": "c" * 64})):
            intent, retained, api, _ = bundle()
            intent[field] = value
            with self.subTest(field=field), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            self.assertFalse(api.calls)
        intent, retained, api, _ = bundle()
        intent.update(operation="inspect", source=None)
        with self.assertRaises(Refusal):
            original.observe(intent, retained, api)
        self.assertFalse(api.calls)

    def test_original_run_fork_branch_event_commit_attempt_and_actor_must_match(self):
        changes = {"id": 124, "run_attempt": 2, "event": "pull_request", "head_branch": "topic",
                   "head_sha": "2" * 40, "path": ".github/workflows/ship.yml",
                   "repository": {"id": 11, "full_name": "PerishLab/wharf"},
                   "head_repository": {"id": 10, "full_name": "other/wharf"},
                   "actor": {"id": 22, "login": "alice"}, "triggering_actor": {"id": 21, "login": "observer"}}
        for key, value in changes.items():
            intent, retained, api, _ = bundle()
            api.observed[key] = value
            with self.subTest(key=key), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            api.jobs.assert_not_called()

    def test_nonterminal_unknown_and_malformed_run_conclusions_refuse(self):
        for status, conclusion in (("in_progress", None), ("queued", None), ("completed", None),
                                   ("completed", "unknown"), ("completed", []), ("completed", {})):
            intent, retained, api, _ = bundle()
            api.observed.update(status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            api.jobs.assert_not_called()

    def test_archived_foreign_and_changed_control_default_branch_refuse(self):
        for change in ({"archived": True}, {"full_name": "other/wharf"}, {"id": True}, {"default_branch": "topic"}):
            intent, retained, api, _ = bundle()
            repository = api.repository("PerishLab/wharf")
            api.calls.clear()
            api.repository = mock.Mock(return_value=dict(repository, **change))
            with self.subTest(change=change), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            api.jobs.assert_not_called()

    def test_incomplete_oversized_and_malformed_inventory_refuses(self):
        for change in ({"total_count": 10}, {"total_count": True}, {"jobs": []}, {"jobs": None},
                       {"total_count": 31, "jobs": [{}] * 31}):
            intent, retained, api, inventory = bundle()
            inventory.update(change)
            with self.subTest(change=change), self.assertRaises(Refusal):
                original.observe(intent, retained, api)

    def test_missing_duplicate_foreign_and_ambiguous_native_jobs_refuse(self):
        for fault in ("missing", "duplicate-id", "duplicate-name", "foreign", "missing-attempt", "alias-duplicate"):
            intent, retained, api, inventory = bundle()
            held = inventory["jobs"]
            if fault == "missing":
                held.pop()
            elif fault == "duplicate-id":
                held[-1]["id"] = held[0]["id"]
            elif fault == "duplicate-name":
                held[-1]["name"] = held[0]["name"]
            elif fault == "foreign":
                held[-1]["name"] = "[deploy] unrecognized writer"
            elif fault == "missing-attempt":
                del held[0]["run_attempt"]
            else:
                inactive = copy.deepcopy(held[6])
                inactive.update(id=100, name="[deploy] cfworker disposal")
                held.append(inactive)
            inventory["total_count"] = len(held)
            with self.subTest(fault=fault), self.assertRaises(Refusal):
                original.observe(intent, retained, api)

    def test_exact_job_run_attempt_branch_head_and_terminal_conclusion_required(self):
        for key, value in (("id", True), ("run_id", 124), ("run_attempt", 2), ("run_attempt", True),
                           ("head_sha", "2" * 40), ("head_branch", "topic"), ("status", "in_progress"),
                           ("conclusion", None), ("conclusion", "unknown"), ("conclusion", []), ("name", None)):
            intent, retained, api, inventory = bundle()
            inventory["jobs"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                original.observe(intent, retained, api)

    def test_inactive_skipped_caller_alias_is_canonicalized_and_active_alias_refuses(self):
        intent, retained, api, inventory = bundle()
        inventory["jobs"][6]["name"] = "[deploy] cfworker disposal"
        result = original.observe(intent, retained, api)
        self.assertIn("[deploy] cfworker disposal / [deploy] cfworker disposal", [job["name"] for job in result["jobs"]])
        for name, conclusion in (("[deploy] cfworker lane", "skipped"), ("[deploy] cfworker disposal", "success"),
                                 ("[deploy] cfworker disposal / [deploy] cfworker disposal", "success")):
            intent, retained, api, inventory = bundle()
            inventory["jobs"][6].update(name=name, conclusion=conclusion)
            with self.subTest(name=name, conclusion=conclusion), self.assertRaises(Refusal):
                original.observe(intent, retained, api)

    def test_run_rerun_or_fact_movement_after_job_read_refuses(self):
        for change in ({"run_attempt": 2}, {"conclusion": "cancelled"}, {"actor": {"id": 22, "login": "observer"}}):
            intent, retained, api, _ = bundle()
            initial = copy.deepcopy(api.observed)
            api.run = mock.Mock(side_effect=[initial, dict(initial, **change)])
            with self.subTest(change=change), self.assertRaises(Refusal):
                original.observe(intent, retained, api)
            self.assertEqual(api.run.call_count, 2)

    def test_provider_failure_stops_without_retry_or_fallback(self):
        intent, retained, api, _ = bundle()
        api.jobs.side_effect = Refusal("native read unavailable")
        with self.assertRaisesRegex(Refusal, "native read unavailable"):
            original.observe(intent, retained, api)
        api.jobs.assert_called_once_with(123, 1)
        self.assertEqual(api.calls[-1], ("run", 123))


if __name__ == "__main__":
    unittest.main()
