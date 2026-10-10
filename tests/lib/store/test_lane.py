import copy
import unittest

from lib.content.lane import codec
from lib.refusal import Conflict, Refusal
from lib.store.lane import documents, registration, state
from tests.lib.identity.test_operation import example
from tests.lib.preview.test_store import Lost, Store

CAPABILITIES = ["inspect", "build", "deploy", "dispose"]


def operation(lane="preview", revision=1, identity="6", expected=None):
    value = example("operation-deploy")
    requested = value["request"]
    requested["context"].update(requested={"repository": "PerishLab/crest", "app": "crest", "lane": lane}, revision=revision, request=identity * 64)
    requested["expected"] = expected or {"state": "absent"}
    value["assessment"]["capabilities"] = CAPABILITIES.copy()
    value["assessment"]["request"] = codec.digest("request", requested)
    value["assessment"]["conditions"]["observed"] = copy.deepcopy(requested["expected"])
    return codec.read("operation", value)


def registered(value):
    return {"schema": "wharf.lane.registration/v1", "declaration": {
        "target": value["request"]["context"]["requested"], "adaptor": "static",
        "capabilities": CAPABILITIES.copy(), "authorities": {"authorization": "ensign", "publication": "wharf"}},
        "mapping": {"provider": "cfworker", "access": "public", "account": "a" * 32, "resource": "crest-development"},
        "generation": "9" * 64, "enabled": True}


def reserve(bucket, value):
    return state.reserve(bucket, value["request"], value["assessment"])


def dispose(previous):
    value = operation(revision=2, identity="7", expected=previous["observed"])
    value["request"]["intent"] = {"action": "dispose"}
    value["assessment"]["request"] = codec.digest("request", value["request"])
    value["outcome"]["material"] = {"state": "absent"}
    return value


class LaneState(unittest.TestCase):
    def setUp(self):
        self.bucket = Store()
        self.value = operation()
        self.registered = registered(self.value)
        registration.write(self.bucket, self.registered, None)

    def test_read_is_absent_without_mutation(self):
        count = len(self.bucket.writes)
        current, etag = state.read(self.bucket, self.registered)
        self.assertEqual((current["revision"], current["phase"], etag), (1, "vacant", None))
        self.assertEqual(len(self.bucket.writes), count)

    def test_default_and_keyed_lanes_are_independent(self):
        first = reserve(self.bucket, self.value)
        second = operation("preview.review")
        registration.write(self.bucket, registered(second), None)
        reserve(self.bucket, second)
        state.record(self.bucket, second)
        self.assertEqual(state.read(self.bucket, self.registered)[0], first)
        self.assertNotEqual(documents.location(first["target"]), documents.location(second["request"]["context"]["requested"]))
        self.assertTrue(all(key.startswith("lane/v1/") for key in self.bucket.objects))

    def test_other_apps_and_repositories_are_independent(self):
        first = reserve(self.bucket, self.value)
        for field, changed in (("app", "other"), ("repository", "PerishLab/other")):
            value = operation()
            value["request"]["context"]["requested"][field] = changed
            value["assessment"]["request"] = codec.digest("request", value["request"])
            registration.write(self.bucket, registered(value), None)
            reserve(self.bucket, value)
        self.assertEqual(state.read(self.bucket, self.registered)[0], first)

    def test_same_request_reservation_and_settlement_are_readbacks(self):
        first = reserve(self.bucket, self.value)
        count = len(self.bucket.writes)
        self.assertEqual(reserve(self.bucket, self.value), first)
        self.assertEqual(len(self.bucket.writes), count)
        settled = state.record(self.bucket, self.value)
        self.assertEqual((settled["revision"], settled["phase"]), (2, "settled"))
        count = len(self.bucket.writes)
        self.assertEqual(state.record(self.bucket, self.value), settled)
        self.assertEqual(reserve(self.bucket, self.value), settled)
        self.assertEqual(len(self.bucket.writes), count)

    def test_competitor_and_stale_requests_never_write(self):
        reserve(self.bucket, self.value)
        count = len(self.bucket.writes)
        self.assertRaises(Refusal, reserve, self.bucket, operation(identity="7"))
        self.assertEqual(len(self.bucket.writes), count)
        state.record(self.bucket, self.value)
        count = len(self.bucket.writes)
        self.assertRaises(Refusal, reserve, self.bucket, operation(identity="7"))
        self.assertEqual(len(self.bucket.writes), count)

    def test_unknown_and_refused_results_never_release_a_writer(self):
        for outcome, reason in (("unknown", "unavailable"), ("refused", "denied")):
            bucket = Store()
            registration.write(bucket, self.registered, None)
            reserve(bucket, self.value)
            uncertain = copy.deepcopy(self.value)
            uncertain["outcome"] = {"outcome": outcome, "reason": reason}
            current = state.record(bucket, uncertain)
            self.assertEqual((current["revision"], current["phase"]), (1, "unknown"))
            self.assertEqual(reserve(bucket, self.value), current)
            self.assertRaises(Refusal, reserve, bucket, operation(identity="7"))
            self.assertEqual(state.record(bucket, self.value)["revision"], 2)

    def test_tombstone_prevents_old_recreation_but_allows_new_exact_request(self):
        reserve(self.bucket, self.value)
        previous = state.record(self.bucket, self.value)
        deletion = dispose(previous)
        reserve(self.bucket, deletion)
        tombstone = state.record(self.bucket, deletion)
        self.assertEqual((tombstone["revision"], tombstone["observed"]), (3, {"state": "absent"}))
        self.assertRaises(Refusal, reserve, self.bucket, self.value)
        self.assertRaises(Refusal, reserve, self.bucket, operation(revision=2, identity="8"))
        fresh = operation(revision=3, identity="8")
        self.assertEqual(reserve(self.bucket, fresh)["revision"], 3)

    def test_lost_reservation_and_completion_responses_are_readable(self):
        lost = Lost()
        lost.objects = self.bucket.objects
        self.assertRaises(OSError, reserve, lost, self.value)
        self.assertEqual(reserve(self.bucket, self.value)["phase"], "reserved")
        self.assertRaises(OSError, state.record, lost, self.value)
        self.assertEqual(state.record(self.bucket, self.value)["phase"], "settled")

    def test_current_cas_conflict_propagates_without_overwrite(self):
        current, etag = state.read(self.bucket, self.registered)
        rival = reserve(self.bucket, operation(identity="7"))
        proposed = state.started(current, {key: self.value[key] for key in ("request", "assessment")}, self.registered)
        self.assertRaises(Conflict, self.bucket.swap, documents.location(current["target"]) + "/current.json", documents.encode(proposed), etag)
        self.assertEqual(state.read(self.bucket, self.registered)[0], rival)

    def test_request_identity_cannot_change_bytes_even_at_new_revision(self):
        reserve(self.bucket, self.value)
        previous = state.record(self.bucket, self.value)
        changed = operation(revision=2, expected=previous["observed"])
        count = len(self.bucket.writes)
        self.assertRaises(Refusal, reserve, self.bucket, changed)
        self.assertEqual(len(self.bucket.writes), count)

    def test_wrong_result_and_mutated_assessment_never_write(self):
        reserve(self.bucket, self.value)
        changed = copy.deepcopy(self.value)
        changed["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
        count = len(self.bucket.writes)
        for value in (operation(identity="7"), changed):
            self.assertRaises(Refusal, state.record, self.bucket, value)
        self.assertEqual(len(self.bucket.writes), count)

    def test_no_current_reservation_means_no_result_permission(self):
        prefix = documents.location(self.registered["declaration"]["target"])
        body = documents.encode({key: self.value[key] for key in ("request", "assessment")})
        documents.immutable(self.bucket, prefix + "/requests/" + self.value["request"]["context"]["request"] + "/intent.json", body)
        self.assertRaises(Refusal, state.record, self.bucket, self.value)
        self.assertEqual(state.read(self.bucket, self.registered)[0]["phase"], "vacant")

    def test_malformed_history_and_numeric_revisions_refuse(self):
        reserve(self.bucket, self.value)
        current = state.record(self.bucket, self.value)
        for key, value in (("revision", True), ("revision", 2.0), ("revision", 0), ("revision", 2 ** 64),
                           ("last", None), ("phase", "vacant"), ("active", {}), ("observed", {"state": "absent"}),
                           ("target", {}), ("registration", "0" * 64), ("schema", "unknown")):
            broken = copy.deepcopy(current)
            broken[key] = value
            with self.subTest(key=key, value=value):
                self.assertRaises(Refusal, state.checked, broken, self.registered)

    def test_request_expected_material_is_not_inferred(self):
        for expected in ({"state": "present", "revision": 1, "digest": "0" * 64},):
            self.assertRaises(Refusal, reserve, self.bucket, operation(expected=expected))

    def test_non_mutating_actions_cannot_reserve_current_state(self):
        for action in ("inspect", "build"):
            value = example("operation-" + action)
            value["request"]["context"]["requested"]["lane"] = "preview"
            value["assessment"]["capabilities"] = CAPABILITIES.copy()
            value["assessment"]["request"] = codec.digest("request", value["request"])
            self.assertRaises(Refusal, reserve, self.bucket, value)

    def test_read_rejects_bad_duplicate_oversized_or_unversioned_document(self):
        key = documents.location(self.registered["declaration"]["target"]) + "/current.json"
        for body in (b"bad", b'{"schema":1,"schema":1}', b" " * 65537):
            self.bucket.put(key, body)
            self.assertRaises(Refusal, state.read, self.bucket, self.registered)
        self.bucket.snapshot = lambda key: (b"{}", None)
        self.assertRaises(Refusal, state.read, self.bucket, self.registered)

    def test_registration_update_pins_old_state_and_never_resets_it(self):
        current = reserve(self.bucket, self.value)
        _, etag = registration.read(self.bucket, current["target"])
        changed = dict(self.registered, generation="8" * 64)
        registration.write(self.bucket, changed, etag)
        count = len(self.bucket.writes)
        self.assertRaises(Refusal, reserve, self.bucket, self.value)
        self.assertRaises(Refusal, state.record, self.bucket, self.value)
        self.assertEqual(len(self.bucket.writes), count)

    def test_registration_requires_explicit_exact_conditional_write(self):
        self.assertRaises(Conflict, registration.write, self.bucket, self.registered, None)
        self.assertRaises(Conflict, registration.write, self.bucket, self.registered, "stale")
        self.assertRaises(Refusal, registration.write, self.bucket, self.registered, "")

    def test_missing_disabled_foreign_or_wrong_authority_registration_refuses(self):
        self.assertRaises(Refusal, reserve, Store(), self.value)
        key = registration.location(self.registered["declaration"]["target"])
        for field, value in (("enabled", False), ("enabled", 1)):
            self.bucket.put(key, documents.encode(dict(self.registered, **{field: value})))
            self.assertRaises(Refusal, reserve, self.bucket, self.value)
        for field, value in (("target", dict(self.registered["declaration"]["target"], lane="preview.other")),
                             ("authorities", {"authorization": "other", "publication": "wharf"})):
            wrong = copy.deepcopy(self.registered)
            wrong["declaration"][field] = value
            self.bucket.put(key, documents.encode(wrong))
            self.assertRaises(Refusal, reserve, self.bucket, self.value)

    def test_unknown_registration_fields_or_unsupported_mapping_refuse(self):
        for key, value in (("credential", "secret"), ("generation", True), ("mapping", {})):
            self.assertRaises(Refusal, registration.checked, dict(self.registered, **{key: value}))
        wrong = copy.deepcopy(self.registered)
        wrong["mapping"]["provider"] = "other"
        self.assertRaises(Refusal, registration.checked, wrong)

    def test_state_and_registration_are_detached_from_caller_objects(self):
        current = reserve(self.bucket, self.value)
        current["active"]["request"]["context"]["request"] = "0" * 64
        actual, _ = state.read(self.bucket, self.registered)
        self.assertEqual(actual["active"]["request"], self.value["request"])

    def test_unknown_disposal_retains_present_material_and_revision(self):
        reserve(self.bucket, self.value)
        previous = state.record(self.bucket, self.value)
        deletion = dispose(previous)
        reserve(self.bucket, deletion)
        deletion["outcome"] = {"outcome": "unknown", "reason": "unverified"}
        current = state.record(self.bucket, deletion)
        self.assertEqual((current["revision"], current["observed"]), (2, previous["observed"]))
        self.assertRaises(Refusal, reserve, self.bucket, operation(revision=2, identity="8", expected=previous["observed"]))

    def test_registration_movement_between_history_and_cas_refuses(self):
        original = self.bucket.create

        def moving(key, body):
            original(key, body)
            registration_key = registration.location(self.registered["declaration"]["target"])
            self.bucket.put(registration_key, documents.encode(dict(self.registered, enabled=False)))

        self.bucket.create = moving
        self.assertRaises(Refusal, reserve, self.bucket, self.value)
        self.assertNotIn(documents.location(self.registered["declaration"]["target"]) + "/current.json", self.bucket.objects)

    def test_uncertain_writer_and_inexact_material_cannot_settle(self):
        reserve(self.bucket, self.value)
        for writer in ("active", "unknown"):
            changed = copy.deepcopy(self.value)
            changed["outcome"]["writer"] = writer
            self.assertRaises(Refusal, state.record, self.bucket, changed)
        changed = copy.deepcopy(self.value)
        changed["outcome"]["material"]["artifact"]["source"]["commit"] = "0" * 40
        self.assertRaises(Refusal, state.record, self.bucket, changed)

    def test_revision_exhaustion_never_creates_an_unsettleable_reservation(self):
        value = operation(revision=2 ** 64 - 2)
        current = state.initial(self.registered)
        current.update(revision=2 ** 64 - 1, phase="settled", last=value, observed=state.observation(value))
        next_value = operation(revision=current["revision"], identity="7", expected=current["observed"])
        self.assertRaises(Refusal, state.started, current, {key: next_value[key] for key in ("request", "assessment")}, self.registered)

    def test_failed_current_cas_leaves_immutable_history_without_permission(self):
        original = self.bucket.swap

        def conflict(key, body, etag):
            raise Conflict("competing snapshot")

        self.bucket.swap = conflict
        self.assertRaises(Conflict, reserve, self.bucket, self.value)
        self.assertIsNone(state.read(self.bucket, self.registered)[0]["active"])
        self.assertRaises(Refusal, state.record, self.bucket, self.value)
        self.bucket.swap = original
        reserve(self.bucket, self.value)
        self.bucket.swap = conflict
        self.assertRaises(Conflict, state.record, self.bucket, self.value)
        self.assertEqual(state.read(self.bucket, self.registered)[0]["phase"], "reserved")
        self.bucket.swap = original
        self.assertEqual(state.record(self.bucket, self.value)["phase"], "settled")
