import io
import json
import unittest
from unittest import mock

from lib.content.static import evidence
from lib.process.cfworker import upload
from lib.refusal import Refusal
from tests.lib.process import test_cfworker_upload
from tests.lib.process.test_cfworker import Response


class Replies(unittest.TestCase):
    def setUp(self):
        self.fixture = test_cfworker_upload.Upload()
        self.fixture.setUp()
        self.secret = test_cfworker_upload.JWT + '\n::error::private-provider-value'

    def refused(self, body, change=None, phase='bucket'):
        api = self.fixture.client()
        if phase == 'bucket':
            self.fixture.session(api)
        def received(request, timeout):
            response = Response(body, request.full_url)
            response.status = 201 if phase == 'bucket' else 200
            if change is not None:
                change(response)
            return response
        api._client.open.side_effect = received
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
            result = self.fixture.bucket(api) if phase == 'bucket' else self.fixture.session(api)
        self.assertFalse(result['acknowledged'])
        self.assertFalse(result['complete'])
        for secret in (self.secret, test_cfworker_upload.JWT, 'private-provider-value', '::error::'):
            self.assertNotIn(secret, stream.getvalue())
        with self.assertRaises(Refusal):
            self.fixture.bucket(api) if phase == 'bucket' else self.fixture.session(api)
        with self.assertRaises(Refusal):
            api.completion(self.fixture.document, self.fixture.bodies.__getitem__)
        self.assertEqual(api._client.open.call_count, 2 if phase == 'bucket' else 1)
        held = json.loads(stream.getvalue())
        self.assertEqual(set(held), {'schema', 'stage', 'category', 'status', 'clause'})
        self.assertEqual(held['schema'], 'wharf.cfworker.diagnostic/v2')
        self.assertEqual(held['category'], 'protocol')
        return held

    def envelope(self, value):
        return json.dumps(value).encode()

    def test_bulk_reply_refusals_distinguish_each_existing_validation_clause(self):
        cases = [(b'[]', 'envelope-type'),
                 (self.envelope({'success': self.secret, 'errors': [], 'result': {}}), 'envelope-success'),
                 (self.envelope({'success': True, 'result': {}}), 'envelope-errors-missing'),
                 (self.envelope({'success': True, 'errors': [self.secret], 'result': {}}), 'envelope-errors'),
                 (self.envelope({'success': True, 'errors': [], 'result': self.secret}), 'result-shape'),
                 (self.envelope({'success': True, 'errors': [], 'result': None}), 'result-shape')]
        for body, clause in cases:
            with self.subTest(clause=clause):
                held = self.refused(body)
                self.assertEqual(held['stage'], 'bucket-response')
                self.assertEqual(held['clause'], clause)
                self.assertEqual(held['status'], 201)

    def test_status_origin_and_encoding_are_distinct_without_logging_values(self):
        cases = [('status', 202, 'response-status', 202),
                 ('status', self.secret, 'response-status', None),
                 ('status', True, 'response-status', None),
                 ('url', 'https://private-provider-value.invalid/' + self.secret, 'response-origin', 201),
                 ('headers', {'Content-Encoding': self.secret}, 'response-encoding', 201)]
        for attribute, value, clause, status in cases:
            with self.subTest(clause=clause, attribute=attribute):
                held = self.refused(b'{}', lambda response: setattr(response, attribute, value))
                self.assertEqual((held['clause'], held['status']), (clause, status))

    def test_invalid_oversized_and_duplicate_json_keep_status_without_body(self):
        bodies = [self.secret.encode(), b'x' * (evidence.LIMIT + 1),
                  b'{"success":true,"success":true,"errors":[],"result":{}}']
        for body in bodies:
            held = self.refused(body)
            self.assertEqual((held['clause'], held['status']), ('response-body', 201))

    def test_session_and_completion_shape_diagnostics_preserve_position_rules(self):
        cases = [('session', {}, 200, 'session-shape', 'session-shape'),
                 ('session', {'jwt': test_cfworker_upload.JWT, 'buckets': []}, 201, 'session-shape', 'session-status'),
                 ('bucket', {}, 200, 'bucket-shape', 'completion-status'),
                 ('bucket', {}, 201, 'bucket-shape', 'completion-shape')]
        for phase, reply, status, stage, clause in cases:
            body = self.envelope({'success': True, 'errors': [], 'result': reply})
            held = self.refused(body, lambda response: setattr(response, 'status', status), phase)
            self.assertEqual((held['stage'], held['clause'], held['status']), (stage, clause, status))

    def test_clauses_are_closed_and_generic_errors_cannot_supply_diagnostics(self):
        for clause in (self.secret, None, [], {}, True):
            with self.assertRaises(Refusal):
                upload.Protocol(clause)
        for status in (self.secret, True, 201.0, 99, 600, {}, None):
            error = Refusal(self.secret)
            error.clause = self.secret
            with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
                upload.diagnostic('bucket-response', error, status)
            held = json.loads(stream.getvalue())
            self.assertIsNone(held['clause'])
            self.assertIsNone(held['status'])
            self.assertNotIn(self.secret, stream.getvalue())

    def test_protocol_mutation_never_forwards_foreign_clause(self):
        error = upload.Protocol('result-shape')
        error.clause = self.secret
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
            upload.diagnostic('bucket-response', error, 201)
        self.assertEqual(json.loads(stream.getvalue())['clause'], None)
        self.assertNotIn(self.secret, stream.getvalue())

    def test_intermediate_status_and_nonempty_result_are_not_completion(self):
        for status, result, clause in [(201, {'jwt': self.secret}, 'intermediate-status'),
                                       (200, {'jwt': self.secret}, 'intermediate-shape')]:
            api = self.fixture.client([[self.fixture.hashes[0]], self.fixture.hashes[1:]])
            self.fixture.session(api)
            self.fixture.response(api, result, status)
            with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
                self.assertFalse(self.fixture.bucket(api)['acknowledged'])
            held = json.loads(stream.getvalue())
            self.assertEqual((held['stage'], held['clause'], held['status']), ('bucket-shape', clause, status))
            self.assertNotIn(self.secret, stream.getvalue())
            with self.assertRaises(Refusal):
                self.fixture.bucket(api, 1)
            self.assertEqual(api._client.open.call_count, 2)

    def test_known_status_survives_read_and_cleanup_failure_without_error_text(self):
        for phase, error, category in [('read', TimeoutError(self.secret), 'timeout'),
                                        ('cleanup', OSError(self.secret), 'transport')]:
            api = self.fixture.client()
            self.fixture.session(api)
            def received(request, timeout):
                body = self.envelope({'success': True, 'errors': [], 'result': {'jwt': test_cfworker_upload.FINAL}})
                reply = Response(body, request.full_url)
                reply.status = 201
                if phase == 'read':
                    reply.read = mock.Mock(side_effect=error)
                    return reply
                held = mock.MagicMock()
                held.__enter__.return_value = reply
                held.__exit__.side_effect = error
                return held
            api._client.open.side_effect = received
            with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
                self.assertFalse(self.fixture.bucket(api)['acknowledged'])
            held = json.loads(stream.getvalue())
            self.assertEqual((held['category'], held['status'], held['clause']), (category, 201, None))
            self.assertNotIn(self.secret, stream.getvalue())
            with self.assertRaises(Refusal):
                api.completion(self.fixture.document, self.fixture.bodies.__getitem__)
            self.assertEqual(api._client.open.call_count, 2)
