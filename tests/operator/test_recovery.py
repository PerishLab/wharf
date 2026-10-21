import copy
import hashlib
import json
import unittest

from lib.content import canonical
from lib.content.lane import codec
from lib.refusal import Conflict, Refusal
from lib.store.lane import documents, recovery, registration, state
from tests.lib.preview.test_store import Store
from tests.lib.store.test_lane import operation, registered, reserve


class Operator(Store):
    def __init__(self):
        super().__init__()
        self.authorizations = []
        self.before = lambda receipt: None

    def authorize(self, receipt):
        self.authorizations.append(copy.deepcopy(receipt))
        self.before(receipt)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.bucket = Operator()
        self.operation = operation()
        self.registered = registered(self.operation)
        registration.write(self.bucket, self.registered, None)
        self.current = reserve(self.bucket, self.operation)
        self.unknown = dict(self.operation, outcome={"outcome": "unknown", "reason": "unavailable"})
        record = {"schema": 2, "entry": {"kind": "operation", "value": self.unknown}}
        self.prefix = documents.location(self.current["target"])
        self.receipt = {"schema": "wharf.lane.operator-recovery/v1", "before": self.current,
                        "precondition": {"digest": hashlib.sha256(self.bucket.get(self.prefix + "/current.json")).hexdigest(), "etag": self.bucket.head(self.prefix + "/current.json")},
                        "unknown": {"reference": {"authority": "wharf", "digest": codec.digest("record", record)}, "record": record},
                        "operation": self.operation, "operator": {"id": 291704231, "login": "LiberteCode"},
                        "evidence": "a" * 64, "reason": "Explicit operator adoption after original producer stopped and exact material readback"}

    def recover(self):
        return state.recover(self.bucket, self.registered, self.receipt)

    def journal(self):
        return self.prefix + "/recovery/" + canonical.digest(self.receipt) + ".json"

    def test_recovery_is_explicit_versioned_and_preserves_original_unknown(self):
        current = self.recover()
        self.assertEqual((current["schema"], current["phase"], current["revision"], current["active"]), ("wharf.lane.current/v2", "settled", 2, None))
        self.assertEqual(current["recovery"]["unknown"]["record"]["entry"]["value"], self.unknown)
        self.assertEqual(current["recovery"]["before"], self.current)
        self.assertEqual(self.bucket.get(self.journal()), documents.encode(self.receipt))
        self.assertEqual(len(self.bucket.authorizations), 2)

    def test_later_update_and_disposal_use_recovered_state_without_recovery_replay(self):
        current = self.recover()
        next_value = operation(revision=2, identity="7", expected=current["observed"])
        reserve(self.bucket, next_value)
        updated = state.record(self.bucket, next_value)
        self.assertEqual(updated["revision"], 3)
        self.assertEqual(updated["recovery"], self.receipt)
        next_value = operation(revision=3, identity="8", expected=updated["observed"])
        next_value["request"]["intent"] = {"action": "dispose"}
        next_value["assessment"]["request"] = codec.digest("request", next_value["request"])
        next_value["outcome"]["material"] = {"state": "absent"}
        reserve(self.bucket, next_value)
        self.assertEqual(state.record(self.bucket, next_value)["revision"], 4)
        self.assertEqual(len(self.bucket.authorizations), 2)

    def test_precondition_binds_actual_original_bytes_including_noncanonical_json(self):
        body = json.dumps(self.current, indent=2).encode()
        self.bucket.swap(self.prefix + "/current.json", body, self.receipt["precondition"]["etag"])
        self.receipt["precondition"] = {"digest": hashlib.sha256(body).hexdigest(), "etag": self.bucket.head(self.prefix + "/current.json")}
        self.assertNotEqual(self.receipt["precondition"]["digest"], canonical.digest(self.current))
        self.assertEqual(self.recover()["revision"], 2)

    def test_actual_unknown_current_is_supported_without_phase_fabrication(self):
        self.current = state.record(self.bucket, self.unknown)
        self.receipt["before"] = self.current
        self.receipt["precondition"] = {"digest": hashlib.sha256(self.bucket.get(self.prefix + "/current.json")).hexdigest(), "etag": self.bucket.head(self.prefix + "/current.json")}
        self.assertEqual(self.recover()["recovery"]["before"]["phase"], "unknown")

    def test_exact_completed_recovery_is_read_only(self):
        current = self.recover()
        count = len(self.bucket.writes)
        self.assertEqual(self.recover(), current)
        self.assertEqual(len(self.bucket.writes), count)
        self.assertEqual(len(self.bucket.authorizations), 2)
        reserve(self.bucket, operation(revision=2, identity="7", expected=current["observed"]))
        with self.assertRaises(Refusal):
            self.recover()

    def test_stale_version_or_moved_source_never_writes(self):
        for field, value in (("etag", "stale"), ("digest", "b" * 64)):
            receipt = copy.deepcopy(self.receipt)
            receipt["precondition"][field] = value
            count = len(self.bucket.writes)
            with self.assertRaises(Refusal):
                state.recover(self.bucket, self.registered, receipt)
            self.assertEqual(len(self.bucket.writes), count)

    def test_unsupported_or_forged_provenance_refuses_before_writes(self):
        for field, value in (("schema", "unknown"), ("operator", {"id": True, "login": "LiberteCode"}), ("reason", ""), ("evidence", "invalid"), ("operation", self.unknown)):
            receipt = copy.deepcopy(self.receipt)
            receipt[field] = value
            count = len(self.bucket.writes)
            with self.assertRaises(Refusal):
                state.recover(self.bucket, self.registered, receipt)
            self.assertEqual(len(self.bucket.writes), count)
        receipt = copy.deepcopy(self.receipt)
        receipt["extra"] = True
        with self.assertRaises(Refusal):
            recovery.checked(receipt)

    def test_operator_revalidation_failure_stops_before_journal(self):
        def refuse(receipt):
            raise Refusal("operator authority or observed material moved")
        self.bucket.before = refuse
        count = len(self.bucket.writes)
        with self.assertRaises(Refusal):
            self.recover()
        self.assertEqual(len(self.bucket.writes), count)

    def test_state_movement_after_journal_stops_before_cas(self):
        def moved(receipt):
            if len(self.bucket.authorizations) == 2:
                state.record(self.bucket, self.unknown)
        self.bucket.before = moved
        with self.assertRaises(Refusal):
            self.recover()
        self.assertTrue(self.bucket.exists(self.journal()))
        self.assertEqual(state.read(self.bucket, self.registered)[0]["phase"], "unknown")

    def test_journal_conflict_cannot_release_original_reservation(self):
        self.bucket.create(self.journal(), b"foreign")
        with self.assertRaises(Refusal):
            self.recover()
        self.assertEqual(state.read(self.bucket, self.registered)[0], self.current)

    def test_cas_conflict_never_retries(self):
        def conflict(key, body, etag):
            raise Conflict("moved")
        self.bucket.swap = conflict
        with self.assertRaises(Conflict):
            self.recover()
        self.assertEqual(state.read(self.bucket, self.registered)[0], self.current)

    def test_lost_cas_response_is_recovered_only_by_exact_readback(self):
        swap = self.bucket.swap
        def lost(key, body, etag):
            swap(key, body, etag)
            raise OSError("response lost")
        self.bucket.swap = lost
        with self.assertRaises(OSError):
            self.recover()
        count = len(self.bucket.writes)
        self.assertEqual(self.recover()["revision"], 2)
        self.assertEqual(len(self.bucket.writes), count)

    def test_current_cannot_erase_or_forge_operator_provenance(self):
        current = self.recover()
        for change in ({"schema": "wharf.lane.current/v1"}, {"recovery": dict(self.receipt, evidence="invalid")}, {"revision": 1}, {"last": self.unknown}):
            with self.assertRaises(Refusal):
                state.checked(dict(current, **change), self.registered)
        with self.assertRaises(Refusal):
            state.recovered(dict(self.registered, enabled=False), self.receipt)
