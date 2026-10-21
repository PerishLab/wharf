import unittest
from unittest import mock

from lib.content.static import admission
from lib.process.lane import entry
from lib.refusal import Refusal
from tests.lib.preview import test_admission
from tests.workflow import fixtures


class Startup(unittest.TestCase):
    def observe(self, api, job="admission", change=None):
        environ = dict(fixtures.platform(), GITHUB_JOB=job,
                       WHARF_PREVIEW_CONTROL_TOKEN="control", WHARF_PREVIEW_PRODUCT_TOKEN="product")
        environ.update(change or {})
        store = fixtures.Store(fixtures.registered())
        with mock.patch.object(entry, "Reader", return_value=store), mock.patch.object(entry, "GitHub", return_value=api):
            return entry.observe(fixtures.request(), environ)

    def test_queued_initial_read_admission_keeps_actual_identity_and_permissions(self):
        api = fixtures.provider()
        api.observed["status"] = "queued"
        observed = self.observe(api)
        self.assertEqual(observed["workflow"]["run"], test_admission.CONTEXT["run"])
        self.assertEqual(observed["actors"]["actor"]["id"], 20)
        self.assertIsNone(observed["current"]["active"])
        self.assertEqual(observed["current"]["phase"], "vacant")
        self.assertFalse(any(call[0] not in ("repository", "run", "permission", "commit") for call in api.calls))

    @mock.patch.object(admission.time, "sleep")
    def test_reservation_and_every_later_role_still_refuse_queued(self, pause):
        for job in ("bind", "build", "verify", "apply", "dispose", "evidence", "state", "unknown", ""):
            api = fixtures.provider()
            api.observed["status"] = "queued"
            with self.subTest(job=job), self.assertRaises(Refusal):
                self.observe(api, job)
        api = fixtures.provider()
        api.observed["status"] = "queued"
        with self.assertRaises(Refusal):
            entry.authority(fixtures.request(), test_admission.CONTEXT, api)

    def test_other_states_and_wrong_run_identity_are_not_startup_lag(self):
        for change in ({"status": "completed"}, {"status": "waiting"}, {"status": "requested"},
                       {"status": "pending"}, {"status": None}, {"run_attempt": 2},
                       {"head_sha": "2" * 40}, {"path": ".github/workflows/ship.yml"},
                       {"event": "pull_request"}, {"head_branch": "topic"},
                       {"head_repository": {"id": 999, "full_name": "attacker/wharf"}}):
            api = fixtures.provider()
            api.observed.update(status="queued")
            api.observed.update(change)
            with self.subTest(change=change), self.assertRaises(Refusal):
                self.observe(api)

    def test_both_actor_permissions_and_trusted_platform_remain_mandatory(self):
        for actor in ("alice", "bob"):
            api = fixtures.provider()
            api.observed["status"] = "queued"
            api.permissions[actor]["permission"] = "read"
            with self.assertRaises(Refusal):
                self.observe(api)
        for change in ({"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_WORKFLOW_SHA": "2" * 40}):
            with self.assertRaises(Refusal):
                self.observe(fixtures.provider(), change=change)

    def test_starting_mode_is_not_a_dispatch_field_or_legacy_preview_option(self):
        for field in ("starting", "GITHUB_JOB"):
            value = dict(fixtures.request(), **{field: True})
            with self.assertRaises(Refusal):
                entry.requested(fixtures.encoded(value))
        api = test_admission.Provider()
        api.observed["status"] = "queued"
        with self.assertRaises(Refusal):
            admission.workflow(api, test_admission.CONTEXT, starting=True)
