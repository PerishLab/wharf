import copy
import unittest
from unittest import mock

from lib.content.static import admission
from lib.process.lane import entry
from lib.refusal import Refusal
from tests.lib.preview import test_admission
from tests.workflow import fixtures


class Metadata(unittest.TestCase):
    @mock.patch.object(admission.time, "sleep")
    def test_queued_then_active_rechecks_before_binding(self, pause):
        api = fixtures.provider()
        active = copy.deepcopy(api.observed)
        queued = dict(active, status="queued")
        with mock.patch.object(api, "run", side_effect=[queued, queued, active]) as reads:
            observed = entry.authority(fixtures.request(), test_admission.CONTEXT, api)
        self.assertEqual(observed["workflow"]["run"], 123)
        self.assertEqual(observed["actors"]["actor"]["id"], 20)
        self.assertEqual(reads.call_count, 3)
        self.assertEqual(pause.call_args_list, [mock.call(10), mock.call(10)])

    @mock.patch.object(admission.time, "sleep")
    def test_exhaustion_is_bounded_and_grants_no_authority(self, pause):
        api = fixtures.provider()
        api.observed["status"] = "queued"
        with mock.patch.object(api, "run", wraps=api.run) as reads, self.assertRaises(Refusal):
            entry.authority(fixtures.request(), test_admission.CONTEXT, api)
        self.assertEqual(reads.call_count, 19)
        self.assertEqual(pause.call_args_list, [mock.call(10)] * 18)
        self.assertFalse(any(call[0] == "permission" for call in api.calls))

    @mock.patch.object(admission.time, "sleep")
    def test_moved_terminal_unknown_and_errors_stop_polling(self, pause):
        for change in ({"status": "completed"}, {"status": "waiting"}, {"status": None},
                       {"run_attempt": 2}, {"id": 124}, {"head_sha": "2" * 40},
                       {"head_repository": {"id": 999, "full_name": "attacker/wharf"}}):
            api = fixtures.provider()
            queued = dict(api.observed, status="queued")
            moved = dict(queued, **change)
            pause.reset_mock()
            with self.subTest(change=change), mock.patch.object(api, "run", side_effect=[queued, moved]), self.assertRaises(Refusal):
                entry.authority(fixtures.request(), test_admission.CONTEXT, api)
            pause.assert_called_once_with(10)
        api = fixtures.provider()
        with mock.patch.object(api, "run", side_effect=[dict(api.observed, status="queued"), Refusal("read failed")]), self.assertRaises(Refusal):
            entry.authority(fixtures.request(), test_admission.CONTEXT, api)

    @mock.patch.object(admission.time, "sleep")
    def test_legacy_and_startup_do_not_poll(self, pause):
        api = test_admission.Provider()
        api.observed["status"] = "queued"
        with self.assertRaises(Refusal):
            admission.workflow(api, test_admission.CONTEXT)
        lane = fixtures.provider()
        lane.observed["status"] = "queued"
        admission.workflow(lane, test_admission.CONTEXT, entry.policy(), True)
        pause.assert_not_called()
