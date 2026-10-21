import io
import json
import unittest
from unittest import mock

from lib.content.static import evidence
from lib.refusal import Refusal
from tests.cfworker import test_deployment
from tests.lib.process.test_cfworker import Response


class Replies(unittest.TestCase):
    def setUp(self):
        self.fixture = test_deployment.Deployment()
        self.fixture.setUp()
        self.secret = 'private-deploy-credential\n::error::private-provider-value'

    def refused(self, body, change=None, plan=None):
        api = self.fixture.client(plan)
        def received(request, timeout):
            response = Response(body, request.full_url)
            response.status = 201
            if change is not None:
                change(response)
            return response
        api._client.open.side_effect = received
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
            result = self.fixture.invoke(api)
        self.assertFalse(result['acknowledged'])
        self.assertIsNone(result['deployment'])
        for value in (self.secret, 'private-provider-value', 'private-deploy-credential', '::error::'):
            self.assertNotIn(value, stream.getvalue())
        with self.assertRaises(Refusal):
            self.fixture.invoke(api)
        self.assertEqual(api._client.open.call_count, 1)
        held = json.loads(stream.getvalue())
        self.assertEqual(set(held), {'schema', 'stage', 'category', 'status', 'clause'})
        self.assertEqual(held['schema'], 'wharf.cfworker.diagnostic/v2')
        self.assertEqual(held['stage'], 'deployment-response')
        return held

    def envelope(self, value):
        return json.dumps({'success': True, 'errors': [], 'result': value}).encode()

    def test_each_envelope_guard_is_finite_and_preserves_known_status(self):
        cases = [(b'[]', 'envelope-type'),
                 (b'{"success":false}', 'envelope-success'),
                 (b'{"success":true,"result":{}}', 'envelope-errors-missing'),
                 (json.dumps({'success': True, 'errors': [self.secret]}).encode(), 'envelope-errors'),
                 (self.envelope(self.secret), 'result-shape'),
                 (self.envelope(None), 'result-shape')]
        for body, clause in cases:
            with self.subTest(clause=clause):
                held = self.refused(body)
                self.assertEqual((held['category'], held['status'], held['clause']), ('protocol', 201, clause))

    def test_status_origin_and_encoding_do_not_emit_provider_values(self):
        cases = [('status', 202, 'response-status', 202),
                 ('status', True, 'response-status', None),
                 ('status', self.secret, 'response-status', None),
                 ('url', 'https://private.invalid/' + self.secret, 'response-origin', 201),
                 ('headers', {'Content-Encoding': self.secret}, 'response-encoding', 201)]
        for attribute, value, clause, status in cases:
            held = self.refused(b'{}', lambda response: setattr(response, attribute, value))
            self.assertEqual((held['clause'], held['status']), (clause, status))

    def test_bounded_json_refusals_keep_status_without_private_body(self):
        for body in (self.secret.encode(), b'x' * (evidence.LIMIT + 1), b'{"success":true,"success":true}'):
            held = self.refused(body)
            self.assertEqual((held['clause'], held['status']), ('response-body', 201))

    def test_missing_foreign_and_retained_identity_remain_refused(self):
        for value in ({}, dict(self.fixture.raw(), preview_id=self.secret), dict(self.fixture.raw(), preview_name=self.secret), {'id': self.fixture.raw()['id']}):
            held = self.refused(self.envelope(value))
            self.assertEqual((held['clause'], held['status']), ('deployment-identity', 201))
        plan = self.fixture.populated()
        held = self.refused(self.envelope(self.fixture.raw(0)), plan=plan)
        self.assertEqual((held['clause'], held['status']), ('deployment-retained', 201))

    def test_read_and_cleanup_failures_keep_status_without_retry(self):
        for phase, error, category in [('read', TimeoutError(self.secret), 'timeout'), ('cleanup', OSError(self.secret), 'transport')]:
            api = self.fixture.client()
            def received(request, timeout):
                response = Response(self.envelope(self.fixture.raw()), request.full_url)
                response.status = 201
                if phase == 'read':
                    response.read = mock.Mock(side_effect=error)
                    return response
                held = mock.MagicMock()
                held.__enter__.return_value = response
                held.__exit__.side_effect = error
                return held
            api._client.open.side_effect = received
            with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
                self.assertFalse(self.fixture.invoke(api)['acknowledged'])
            held = json.loads(stream.getvalue())
            self.assertEqual((held['category'], held['status'], held['clause']), (category, 201, None))
            self.assertNotIn(self.secret, stream.getvalue())
            with self.assertRaises(Refusal):
                self.fixture.invoke(api)
            self.assertEqual(api._client.open.call_count, 1)
