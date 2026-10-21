import json
import unittest
from unittest import mock

from lib.process.lane import execution
from lib.refusal import Refusal
from tests.lib.process import test_cfworker_upload
from tests.lib.process.test_cfworker import Response
from tests.lib.preview.test_admission import CONTEXT
from tests.workflow.settlement import fixtures


class Completion(unittest.TestCase):
    def fixture(self):
        held = test_cfworker_upload.Upload()
        held.setUp()
        api = held.client()
        held.session(api)
        return held, api

    def test_successful_status_requires_typed_completion_without_replay(self):
        for status in (200, 201, 202):
            held, api = self.fixture()
            body = json.dumps({"success": True, "result": {"jwt": test_cfworker_upload.FINAL}}).encode()
            def received(request, timeout):
                response = Response(body, request.full_url)
                response.status = status
                return response
            api._client.open.side_effect = received
            with self.subTest(status=status):
                self.assertTrue(held.bucket(api)["complete"])
                self.assertEqual(api.completion(held.document, held.bodies.__getitem__), test_cfworker_upload.FINAL)
                with self.assertRaises(Refusal):
                    held.bucket(api)
                self.assertEqual(api._client.open.call_count, 2)

    def test_accepted_incomplete_or_malformed_reply_never_completes(self):
        for reply in (None, {}, {"jwt": "bad"}, {"jwt": test_cfworker_upload.FINAL, "extra": 1}):
            held, api = self.fixture()
            held.response(api, reply, 202)
            with self.subTest(reply=reply), mock.patch('sys.stderr'):
                self.assertFalse(held.bucket(api)["acknowledged"])
                with self.assertRaises(Refusal):
                    api.completion(held.document, held.bodies.__getitem__)
                with self.assertRaises(Refusal):
                    held.bucket(api)
                self.assertEqual(api._client.open.call_count, 2)

    def test_intermediate_accepted_bucket_does_not_issue_completion(self):
        held = test_cfworker_upload.Upload()
        held.setUp()
        api = held.client([[held.hashes[0]], held.hashes[1:]])
        held.session(api)
        held.response(api, None, 202)
        self.assertTrue(held.bucket(api)["acknowledged"])
        with self.assertRaises(Refusal):
            api.completion(held.document, held.bodies.__getitem__)
        held.response(api, {"jwt": test_cfworker_upload.FINAL}, 202)
        self.assertTrue(held.bucket(api, 1)["complete"])

    def test_read_only_admission_metadata_is_not_a_lane_writer(self):
        for status in ("queued", "in_progress"):
            held = fixtures.bundle()
            held['jobs']['jobs'][0].update(status=status, conclusion=None)
            with self.subTest(status=status):
                self.assertEqual(execution.native_jobs({'operation': 'apply'}, CONTEXT, held['api']), held['jobs'])
        for status in ("completed", "waiting", None):
            held = fixtures.bundle()
            held['jobs']['jobs'][0].update(status=status, conclusion=None)
            with self.subTest(status=status), self.assertRaises(Refusal):
                execution.native_jobs({'operation': 'apply'}, CONTEXT, held['api'])

    def test_all_other_producers_and_exact_read_job_identity_remain_strict(self):
        for index in range(1, 8):
            held = fixtures.bundle()
            held['jobs']['jobs'][0].update(status='in_progress', conclusion=None)
            held['jobs']['jobs'][index].update(status='in_progress', conclusion=None)
            with self.subTest(index=index), self.assertRaises(Refusal):
                execution.native_jobs({'operation': 'apply'}, CONTEXT, held['api'])
        for field, value in (('run_id', 456), ('head_sha', '9' * 40), ('run_attempt', 2), ('conclusion', 'failure')):
            held = fixtures.bundle()
            held['jobs']['jobs'][0].update(status='in_progress', conclusion=None)
            held['jobs']['jobs'][0][field] = value
            with self.subTest(field=field), self.assertRaises(Refusal):
                execution.native_jobs({'operation': 'apply'}, CONTEXT, held['api'])
