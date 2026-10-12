import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import discard, writer
from lib.refusal import Refusal
from lib.store.lane import public, publication, state
from tests.lib.identity import test_publication
from tests.lib.process import test_cfworker_discard
from tests.lib.store.memory import Memory


class Disposal(unittest.TestCase):
    def setUp(self):
        self.fixture = test_cfworker_discard.Discard()
        self.fixture.setUp()
        self.plan = self.fixture.plan
        self.registered = self.plan["registration"]

    def uncertain(self, reason="unavailable"):
        operation = discard.unknown(self.plan, reason)
        return state.finished(self.plan["current"], operation, self.registered)

    def recorded(self, operation):
        return {"schema": 2, "entry": {"kind": "operation", "value": operation}}

    def test_unknown_dispose_continuation_preserves_exact_original_reservation(self):
        for reason in ("unavailable", "unverified"):
            current = self.uncertain(reason)
            resumed = discard.resume(self.plan, current)
            self.assertEqual(resumed["current"], current)
            self.assertEqual(resumed["before"], self.plan["before"])
            self.assertEqual(current["revision"], self.plan["current"]["revision"])
            self.assertEqual(current["observed"], self.plan["current"]["observed"])
            self.assertEqual(current["active"], self.plan["current"]["active"])
            with self.assertRaises(Refusal):
                writer.Writer("prepared", resumed)
            settled = discard.settle(resumed, self.fixture.absent(), "idle")
            self.assertEqual(settled["request"], current["active"]["request"])

    def test_reserved_refused_settled_and_erased_history_do_not_qualify_as_unknown_recovery(self):
        refused = dict(self.plan["current"]["active"], outcome={"outcome": "refused", "reason": "denied"})
        completed = discard.settle(self.plan, self.fixture.absent(), "idle")
        for current in (self.plan["current"], state.finished(self.plan["current"], refused, self.registered),
                        state.finished(self.plan["current"], completed, self.registered), dict(self.uncertain(), last=None)):
            with self.assertRaises(Refusal):
                discard.resume(self.plan, current)

    def test_competing_valid_request_or_assessment_cannot_replace_original(self):
        for field in ("request", "assessment"):
            current = self.uncertain()
            active = current["active"]
            if field == "request":
                active["request"]["context"]["request"] = "8" * 64
                active["assessment"]["request"] = codec.digest("request", active["request"])
            else:
                active["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
            current["last"] = dict(copy.deepcopy(active), outcome={"outcome": "unknown", "reason": "unavailable"})
            state.checked(current, self.registered)
            with self.assertRaises(Refusal):
                discard.resume(self.plan, current)

    def test_registration_revision_material_or_private_field_movement_refuses(self):
        for field, value in (("revision", 3), ("registration", "0" * 64), ("observed", {"state": "absent"}),
                             ("target", {}), ("extra", "private")):
            with self.assertRaises(Refusal):
                discard.resume(self.plan, dict(self.uncertain(), **{field: value}))
        for reason in ("timeout", "denied", "conflict", None, {}):
            with self.assertRaises(Refusal):
                discard.unknown(self.plan, reason)

    def test_response_loss_recovery_requires_idle_and_unchanged_siblings(self):
        api = self.fixture.client()
        api._client.open.side_effect = TimeoutError("private")
        self.assertFalse(api.delete(self.plan["before"])["acknowledged"])
        with self.assertRaises(Refusal):
            api.delete(self.plan["before"])
        resumed = discard.resume(self.plan, self.uncertain())
        for condition in ("active", "unknown", True, None, "timeout"):
            with self.assertRaises(Refusal):
                discard.settle(resumed, self.fixture.absent(), condition)
        with self.assertRaises(Refusal):
            discard.settle(resumed, dict(self.fixture.absent(), previews=[]), "idle")
        self.assertEqual(discard.settle(resumed, self.fixture.absent(), "idle")["outcome"]["material"], {"state": "absent"})
        self.assertEqual(api._client.open.call_count, 1)

    def test_parallel_discard_calls_consume_exactly_one_attempt(self):
        api = self.fixture.client()
        def attempt(number):
            try:
                return api.delete(self.plan["before"])["acknowledged"]
            except Refusal:
                return False
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sorted(executor.map(attempt, range(4))), [False, False, False, True])
        self.assertEqual(api._client.open.call_count, 1)

    def test_public_absence_binds_exact_original_dispose_and_assessment(self):
        resumed = discard.resume(self.plan, self.uncertain())
        operation = discard.settle(resumed, self.fixture.absent(), "idle")
        record = self.recorded(operation)
        response = test_publication.Response(codec.encode("record", record))
        connection = test_publication.Connection(response)
        public_reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        bucket = Memory()
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(bucket, public_reader, self.registered["declaration"], record)
            verified = discard.published(resumed, public_reader, reference)
            self.assertEqual(verified, operation)
            self.assertEqual(state.finished(resumed["current"], verified, self.registered)["observed"], {"state": "absent"})
            changed = copy.deepcopy(operation)
            changed["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
            changed_record = self.recorded(changed)
            response.body = codec.encode("record", changed_record)
            changed_reference = public_reader.reference(codec.digest("record", changed_record))
            publication.settle(public_reader, self.registered["declaration"], changed_reference, operation["request"])
            with self.assertRaises(Refusal):
                discard.published(resumed, public_reader, changed_reference)

    def test_unknown_public_record_or_unavailable_public_origin_never_settles_absence(self):
        operation = discard.unknown(self.plan, "unverified")
        record = self.recorded(operation)
        response = test_publication.Response(codec.encode("record", record))
        connection = test_publication.Connection(response)
        public_reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(Memory(), public_reader, self.registered["declaration"], record)
            with self.assertRaises(Refusal):
                discard.published(self.plan, public_reader, reference)
            response.status = 404
            with self.assertRaises(Refusal):
                discard.published(self.plan, public_reader, reference)

    def test_detached_recovery_does_not_mutate_original_current_or_before(self):
        resumed = discard.resume(self.plan, self.uncertain())
        resumed["before"]["previews"].clear()
        resumed["current"]["active"]["assessment"]["capabilities"].clear()
        self.assertEqual(len(self.plan["before"]["previews"]), 3)
        self.assertEqual(len(self.plan["current"]["active"]["assessment"]["capabilities"]), 4)
