import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from lib.content import canonical
from lib.process.lane import settlement
from lib.refusal import Refusal
from lib.store.lane import documents, state
from tests.lib.store import test_lane
from tests.operator import test_recovery


class Transport(unittest.TestCase):
    def prepared(self):
        fixture = test_recovery.Recovery()
        fixture.setUp()
        current = fixture.recover()
        operation = test_lane.operation(revision=2, identity="7", expected=current['observed'])
        reserved = test_lane.reserve(fixture.bucket, operation)
        prepared = {'request': operation['request'], 'assessment': operation['assessment'],
                    'registration': fixture.registered, 'current': documents.decode(canonical.encode(reserved))}
        before = {'execution': {'prepared': prepared}}
        env = {'WHARF_LANE_READER_ENDPOINT': 'https://state.example.test', 'WHARF_LANE_READER_BUCKET': 'lane-state'}
        configured = SimpleNamespace(host='state.example.test', name='lane-state', credentials=None)
        with mock.patch.object(settlement.publication, 'configured', return_value=configured), mock.patch.object(settlement, 'Bucket', return_value=fixture.bucket):
            writer = settlement.Writer(before, {'operation': operation}, env)
        writer.authorize = mock.Mock()
        return fixture, operation, writer

    def test_state_record_crosses_sorted_transport_with_exact_recovery_preserved(self):
        fixture, operation, writer = self.prepared()
        actual, _ = state.read(fixture.bucket, fixture.registered)
        transported = writer.before['execution']['prepared']['current']
        self.assertEqual(actual, transported)
        self.assertNotEqual(documents.encode(actual), documents.encode(transported))
        updated = state.record(writer, operation)
        self.assertEqual((updated['phase'], updated['revision'], updated['active']), ('settled', 3, None))
        self.assertEqual(updated['recovery'], fixture.receipt)
        self.assertEqual(state.read(fixture.bucket, fixture.registered)[0], updated)
        self.assertEqual(writer.authorize.call_count, 2)

    def test_other_changes_duplicates_foreign_keys_and_stale_etags_refuse(self):
        fixture, operation, writer = self.prepared()
        current, etag = state.read(fixture.bucket, fixture.registered)
        proposed = state.finished(current, operation, fixture.registered)
        writer.etag = etag
        changed = copy.deepcopy(proposed)
        changed['recovery']['operator']['login'] = 'AnotherActor'
        duplicate = documents.encode(proposed)[:-1] + b',"revision":3}'
        for key, body, version in ((fixture.prefix+'/current.json', documents.encode(changed), etag),
                                   (fixture.prefix+'/foreign.json', documents.encode(proposed), etag),
                                   (fixture.prefix+'/current.json', documents.encode(proposed), 'stale'),
                                   (fixture.prefix+'/current.json', duplicate, etag)):
            with self.subTest(key=key, version=version), self.assertRaises(Refusal):
                writer.swap(key, body, version)
        writer.authorize.assert_not_called()
        self.assertEqual(state.read(fixture.bucket, fixture.registered)[0], current)
