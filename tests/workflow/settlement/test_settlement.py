import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.lane import entry, execution, settlement
from lib.refusal import Conflict, Refusal
from lib.store.lane import documents, public, state
from tests.workflow.publication import fixtures as evidence
from tests.workflow.settlement import fixtures


class Settlement(unittest.TestCase):
    def run_held(self, held):
        with fixtures.runtime(held):
            return settlement.perform(held["held"], held["env"])

    def writes(self, held):
        return [call for call in held["store"].calls if call[0] in ("create", "swap")]

    def test_apply_and_dispose_require_public_read_before_every_conditional_write(self):
        for action in ("apply", "dispose"):
            held = fixtures.bundle(action)
            original = held["store"].create
            reads = []

            def create(key, body):
                reads.append(len(held["reads"]))
                original(key, body)

            held["store"].create = create
            result = self.run_held(held)
            self.assertEqual(result["phase"], "settled")
            self.assertEqual(result["revision"], held["value"]["current"]["revision"] + 1)
            self.assertEqual(held["store"].current["last"], held["operation"])
            self.assertIsNone(held["store"].current["active"])
            self.assertEqual([call[0] for call in self.writes(held)], ["create", "swap"])
            self.assertGreaterEqual(reads[0], 2)
            self.assertGreaterEqual(len(held["reads"]), 3)
            self.assertTrue(all(method == "GET" and headers == {"Accept": "application/json", "Accept-Encoding": "identity"} for method, _, headers in held["reads"]))
            held["api"].jobs.assert_called_with(123, 1)

    def test_inspect_is_read_only_and_never_constructs_a_writer(self):
        held = fixtures.bundle("inspect")
        with fixtures.runtime(held), mock.patch.object(settlement, "Writer", side_effect=AssertionError("writer")):
            self.assertEqual(settlement.perform(held["held"], held["env"])["phase"], "observed")
        self.assertFalse(self.writes(held))

    def test_non_applied_preserves_exact_reservation_revision_and_previous_material(self):
        for action in ("apply", "dispose"):
            for outcome in ("unknown", "refused"):
                held = fixtures.bundle(action, outcome)
                before = copy.deepcopy(held["store"].current)
                original = copy.deepcopy(held["store"].objects)
                with self.subTest(action=action, outcome=outcome), self.assertRaises(Refusal):
                    self.run_held(held)
                self.assertEqual(held["store"].current, before)
                self.assertEqual(held["store"].objects, original)
                self.assertFalse(self.writes(held))

    def test_missing_malformed_foreign_or_wrong_assessment_reference_never_writes(self):
        for failure in ("missing", "malformed", "assessment", "request", "authority", "digest"):
            held = fixtures.bundle()
            if failure == "missing":
                held["held"]["reference"] = ""
            elif failure == "malformed":
                held["held"]["reference"] = '{"digest":"x","digest":"y"}'
            elif failure in ("assessment", "request"):
                operation = copy.deepcopy(held["operation"])
                if failure == "assessment":
                    operation["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "9" * 64
                else:
                    operation["request"]["context"]["request"] = "9" * 64
                    operation["assessment"]["request"] = codec.digest("request", operation["request"])
                record = {"schema": 2, "entry": {"kind": "operation", "value": operation}}
                reference = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"}).reference(codec.digest("record", record))
                held["bucket"].objects[public.location(reference)] = codec.encode("record", record)
                held["held"]["reference"] = documents.encode(reference).decode()
            else:
                reference = documents.decode(held["held"]["reference"].encode())
                reference[failure] = "other" if failure == "authority" else "9" * 64
                held["held"]["reference"] = documents.encode(reference).decode()
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.run_held(held)
            self.assertFalse(self.writes(held))

    def test_unavailable_transformed_or_oversized_public_reply_preserves_state(self):
        for response in (evidence.Response(b"{}", 404), evidence.Response(b"x" * 65537),
                         evidence.Response(b"{}", encoding="gzip"), evidence.Response(b'{"schema":2,"schema":2}')):
            held = fixtures.bundle()
            held["response"] = response
            before = copy.deepcopy(held["store"].current)
            with self.assertRaises(Refusal):
                self.run_held(held)
            self.assertEqual(held["store"].current, before)
            self.assertFalse(self.writes(held))

    def test_public_failure_after_history_before_cas_keeps_reservation(self):
        held = fixtures.bundle()
        before = copy.deepcopy(held["store"].current)
        original = held["store"].create

        def create(key, body):
            original(key, body)
            held["response"] = evidence.Response(b"{}", 503)

        held["store"].create = create
        with self.assertRaises(Refusal):
            self.run_held(held)
        self.assertEqual(held["store"].current, before)
        self.assertEqual([call[0] for call in self.writes(held)], ["create"])

    def test_cas_conflict_or_ambiguous_response_is_one_attempt_never_repair(self):
        for error in (Conflict("moved"), OSError("unknown")):
            held = fixtures.bundle()
            before = copy.deepcopy(held["store"].current)
            held["store"].swap = mock.Mock(side_effect=error)
            with self.assertRaises(Refusal):
                self.run_held(held)
            held["store"].swap.assert_called_once()
            self.assertEqual(held["store"].current, before)
            self.assertEqual(len(self.writes(held)), 1)

    def test_acknowledged_write_without_independent_state_readback_cannot_succeed(self):
        held = fixtures.bundle()
        held["store"].swap = mock.Mock()
        with self.assertRaises(Refusal):
            self.run_held(held)
        held["store"].swap.assert_called_once()

    def test_final_authority_read_movement_cannot_hide_behind_previous_state_readback(self):
        held = fixtures.bundle()
        original = held["api"].permission

        def permission(repository, login):
            result = original(repository, login)
            if held["store"].current["phase"] == "settled":
                held["store"].current["revision"] += 1
            return result

        held["api"].permission = permission
        with self.assertRaises(Refusal):
            self.run_held(held)
        self.assertEqual(sum(call[0] == "swap" for call in held["store"].calls), 1)

    def test_ambiguous_applied_cas_retains_result_but_no_false_success_or_replay(self):
        held = fixtures.bundle()
        original = held["store"].swap

        def swap(key, body, etag):
            original(key, body, etag)
            raise OSError("response lost")

        held["store"].swap = mock.Mock(side_effect=swap)
        with self.assertRaises(Refusal):
            self.run_held(held)
        self.assertEqual(held["store"].current["phase"], "settled")
        with self.assertRaises(Refusal):
            self.run_held(held)
        self.assertEqual(held["store"].swap.call_count, 1)

    def test_dispose_retains_tombstone_history_and_refuses_stale_apply(self):
        held = fixtures.bundle("dispose")
        self.run_held(held)
        self.assertEqual(held["store"].current["observed"], {"state": "absent"})
        self.assertTrue(any(key.endswith(".json") and not key.endswith("custody.json") for key in held["store"].objects))
        with self.assertRaises(Refusal):
            entry.admit(held["intent"], held["store"], {"run": 123, "attempt": 1, "commit": "1" * 40}, held["api"])
        with self.assertRaises(Refusal):
            state.started(held["store"].current, fixtures.bundle()["value"]["current"]["active"], held["value"]["registration"])


if __name__ == "__main__":
    unittest.main()
