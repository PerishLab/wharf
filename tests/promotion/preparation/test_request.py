import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.process.promotion import preflight
from lib.refusal import Refusal

SELECTED = {"repository": "PerishLab/concord", "marker": "v0.58.0", "reference": "v0.58.0-rc.1", "prepare": True}
OBSERVER = {"run": "7", "attempt": "1", "wharf": "c" * 40}


class Request(unittest.TestCase):
    def test_world_is_prospective_closed_and_not_a_publishing_or_replay_request(self):
        value = {"kind": "world", "schema": "wharf.release.semantics.request/v1", "commit": "a" * 40, "scenes": {"fixture": "selected"}}
        selected = {"repository": "PerishLab/plumb", "marker": "v0.141.0-rc.1", "semantics": json.dumps(value, sort_keys=True, separators=(",", ":"))}
        self.assertEqual(preflight.semantics(selected["semantics"]), value)
        held = {"observer": OBSERVER, "selection": json.dumps({"schema": preflight.SCHEMA, "inputs": selected})}
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [])), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(preflight, "qualify") as qualify, mock.patch.object(preflight.parameters, "answer") as answer:
            self.assertFalse(preflight.request(held)["publication"])
            answer.assert_called_once_with({"mode": "world", "preparation": "null"})
            qualify.assert_not_called()
        for changed in ({"commit": "main"}, {"commit": ""}, {"scenes": {}}, {"authorized": True}, {"review": "caller"}, {"same": True}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.semantics(json.dumps(value | changed, sort_keys=True, separators=(",", ":")))
        for changed in ({"prepare": True}, {"candidate": True}, {"run": "9"}, {"reference": "v0.141.0-rc.1"}, {"case": "writer"}, {"repository": "PerishLab/keel"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.selection(selected | changed)

    def test_adoption_is_an_exact_closed_request_with_its_own_mode(self):
        request = {"kind": "adoption", "schema": "wharf.release.semantics.request/v1", "scenes": {"fixture": "selected"}, "review": "https://github.com/PerishLab/seatrial/issues/75#issuecomment-7"}
        selected = {"repository": "PerishLab/plumb", "marker": "v1.2.3", "semantics": json.dumps(request, sort_keys=True, separators=(",", ":"))}
        self.assertEqual(preflight.semantics(selected["semantics"]), request)
        held = {"observer": OBSERVER, "selection": json.dumps({"schema": preflight.SCHEMA, "inputs": selected})}
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [])), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(preflight.parameters, "answer") as answer:
            self.assertFalse(preflight.request(held)["publication"])
            answer.assert_called_once_with({"mode": "adoption", "preparation": "null"})
        for changed in ({"review": ""}, {"scenes": {}}, {"authorized": True}, {"world": {}}):
            value = request | changed
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.semantics(json.dumps(value, sort_keys=True, separators=(",", ":")))
        for malformed in ("{", "[]", "null"):
            with self.subTest(malformed=malformed), self.assertRaises(Refusal):
                preflight.semantics(malformed)

    def test_semantics_capture_is_canonical_stable_and_separate(self):
        request = '{"kind":"capture","schema":"wharf.release.semantics.request/v1"}'
        selected = {"repository": "PerishLab/plumb", "marker": "v1.2.3", "semantics": request}
        self.assertEqual(preflight.selection(selected)["semantics"], request)
        for changed in ({"repository": "PerishLab/keel"}, {"marker": "v1.2.3-rc.1"}, {"run": "7"}, {"prepare": True}, {"candidate": True}, {"proof": True}, {"reference": "v1.2.3-rc.1"}, {"case": "diagnostic"}, {"attempt": "2"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.selection(selected | changed)
        for value in (request + " ", request.replace('"capture"', '"adopt"'), request.replace('"kind":"capture"', '"kind":"capture","kind":"capture"'), '{"kind":"capture"}', "{}", "x" * 65537):
            with self.subTest(value=value[:80]), self.assertRaises(Refusal):
                preflight.selection(selected | {"semantics": value})

    def test_ordinary_defaults_and_native_boolean_transport(self):
        ordinary = preflight.selection({"repository": "PerishLab/concord", "marker": "v0.58.0-rc.1"})
        self.assertFalse(ordinary["prepare"])
        self.assertFalse(ordinary["candidate"])
        self.assertEqual(preflight.selection(dict(SELECTED, prepare="true", candidate="false")), preflight.selection(SELECTED))

    def test_conflicting_modes_and_nonstable_preparation_refuse(self):
        for changed in ({"candidate": True}, {"run": "7"}, {"case": "diagnostic"}, {"attempt": "2"}, {"marker": "v0.58.0-rc.2"}, {"reference": ""}, {"reference": "v0.57.0"}, {"prepare": 1}, {"foreign": True}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.selection(SELECTED | changed)

    def test_projection_cannot_replace_native_event_and_ordinary_never_qualifies(self):
        held = {"observer": OBSERVER, "selection": json.dumps({"schema": preflight.SCHEMA, "inputs": SELECTED})}
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [])), mock.patch.object(preflight, "event", return_value=preflight.selection(SELECTED | {"prepare": False})), mock.patch.object(preflight, "qualify") as qualify:
            with self.assertRaises(Refusal):
                preflight.request(held)
            qualify.assert_not_called()
            held["selection"] = json.dumps({"schema": preflight.SCHEMA, "inputs": SELECTED | {"prepare": False}})
            with mock.patch.object(preflight.parameters, "answer"):
                self.assertIsNone(preflight.request(held)["preparation"])
            qualify.assert_not_called()

    def test_native_event_is_normalized_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"inputs": SELECTED | {"prepare": "true"}}))
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path)}):
                self.assertEqual(preflight.event(), preflight.selection(SELECTED))
                path.write_bytes(b" " * 1048577)
                with self.assertRaises(Refusal):
                    preflight.event()

    def world(self):
        repository = {"id": 1374358737, "node_id": "R_kgDOUesM0Q", "full_name": "PerishLab/wharf", "default_branch": "main"}
        run = {"id": 7, "run_attempt": 1, "head_sha": OBSERVER["wharf"], "repository": repository, "head_repository": repository, "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "head_branch": "main", "status": "in_progress", "conclusion": None}
        job = {"id": 9, "run_id": 7, "run_attempt": 1, "head_sha": OBSERVER["wharf"], "head_branch": "main", "name": "[verify] request", "status": "in_progress", "conclusion": None, "started_at": "2026-11-02T00:00:00Z"}
        return repository, run, {"total_count": 1, "jobs": [job]}

    def test_queued_metadata_waits_without_repeating_any_mutation(self):
        repository, run, jobs = self.world()
        responses = [repository, {"name": "main", "protected": True}, dict(run, status="queued"), run, jobs]
        with mock.patch.dict(os.environ, {"GITHUB_JOB": "request"}), mock.patch.object(preflight.control, "api", side_effect=responses), mock.patch.object(preflight.time, "sleep") as sleep:
            self.assertEqual(preflight.active(OBSERVER, "request"), (OBSERVER, jobs["jobs"]))
            sleep.assert_called_once_with(10)

    def test_foreign_attempt_fork_incomplete_jobs_and_inactive_role_refuse(self):
        repository, run, jobs = self.world()
        alternatives = [(dict(run, run_attempt=2), jobs), (dict(run, head_repository={"id": 1}), jobs), (run, dict(jobs, total_count=2)), (run, {"total_count": 1, "jobs": [dict(jobs["jobs"][0], status="completed", conclusion="success")]})]
        for native, catalog in alternatives:
            responses = [repository, {"name": "main", "protected": True}, native, catalog]
            with self.subTest(native=native), mock.patch.dict(os.environ, {"GITHUB_JOB": "request"}), mock.patch.object(preflight.control, "api", side_effect=responses), self.assertRaises(Refusal):
                preflight.active(OBSERVER, "request")

    def test_current_qualification_and_intent_are_revalidated(self):
        source = {"repository": "PerishLab/concord", "marker": "v0.58.0", "commit": "a" * 40, "tree": "b" * 40}
        intent = {"schema": preflight.INTENT, "source": source, "tag": "d" * 40, "reference": "v0.58.0-rc.1", "publisher": OBSERVER}
        qualified = {"source": source | {"marker": intent["reference"]}, "world": {"current": True}}
        with mock.patch.object(preflight, "intent", return_value=intent), mock.patch.object(preflight.reader, "check"), mock.patch.object(preflight.reader, "pins", return_value=qualified):
            value = preflight.qualify(SELECTED)
        with mock.patch.object(preflight, "qualify", return_value=value) as qualify:
            self.assertEqual(preflight.confirm(value, source), value)
            qualify.assert_called_once()
        changed = copy.deepcopy(value)
        changed["qualification"]["world"]["current"] = False
        with mock.patch.object(preflight, "qualify", return_value=changed), self.assertRaises(Refusal):
            preflight.confirm(value, source)

    def test_explicit_proof_replay_has_a_separate_bounded_request(self):
        requested = {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1", "run": "7", "proof": True}
        self.assertTrue(preflight.selection(requested)["proof"])
        self.assertFalse(preflight.selection({"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"})["proof"])
        self.assertEqual(preflight.selection(requested), preflight.selection(requested | {"proof": "true"}))
        for changed in ({"run": ""}, {"prepare": True}, {"candidate": True}, {"case": "diagnostic"}, {"attempt": "2"}, {"proof": 1}, {"proof": "yes"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.selection(requested | changed)
