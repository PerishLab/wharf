import copy
import unittest

from lib.content import canonical
from lib.refusal import Conflict, Refusal
from lib.store import preview
from lib.store.r2 import Bucket
from tests.lib.content.test_preview import intent, result, target
from tests.lib.store.memory import Memory


class Store(Memory):
    def snapshot(self, key):
        return (self.get(key), self.head(key)) if self.exists(key) else (None, None)


class Lost(Store):
    def swap(self, key, body, etag):
        super().swap(key, body, etag)
        raise OSError("response lost")


def unknown(request):
    value = result(request, "unknown")
    value["provider"].update(state="unknown", deployment=None, quiescent=False)
    value["content"].update(state="unknown", digest=None)
    value["failure"] = "response lost; writer may still run"
    return value


def discarded(request):
    value = result(request, "discarded")
    value["provider"].update(state="absent", deployment=None)
    value["content"].update(state="gone", digest=None)
    return value


class Coordination(unittest.TestCase):
    def test_read_absence_does_not_write(self):
        bucket = Store()
        current, etag = preview.read(bucket, intent("inspect"))
        self.assertEqual((current["phase"], current["revision"], etag), ("absent", 0, None))
        self.assertEqual(bucket.writes, [])

    def test_reserve_and_duplicate_are_idempotent(self):
        bucket = Store()
        current = preview.reserve(bucket, intent(), target())
        count = len(bucket.writes)
        self.assertEqual(preview.reserve(bucket, intent(), target()), current)
        self.assertEqual(len(bucket.writes), count)
        self.assertEqual(current["revision"], 0)

    def test_busy_refuses_other_requests_and_inspect(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        for request in (intent(identity="2"), intent("discard", identity="2"), intent("inspect")):
            with self.subTest(operation=request["operation"]), self.assertRaises(Refusal):
                preview.reserve(bucket, request, target())

    def test_verified_records_exact_and_last_verified(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        current = preview.record(bucket, intent(), result(intent()))
        self.assertEqual((current["revision"], current["phase"], current["active"]), (1, "verified", None))
        self.assertEqual(current["actual"], current["last_verified"])
        count = len(bucket.writes)
        self.assertEqual(preview.record(bucket, intent(), result(intent())), current)
        self.assertEqual(preview.reserve(bucket, intent(), target()), current)
        self.assertEqual(len(bucket.writes), count)

    def test_unknown_holds_reservation_until_explicit_evidence(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        current = preview.record(bucket, intent(), unknown(intent()))
        self.assertEqual((current["revision"], current["phase"], current["active"]), (0, "unknown", intent()))
        self.assertEqual(preview.reserve(bucket, intent(), target()), current)
        with self.assertRaises(Refusal):
            preview.reserve(bucket, intent(identity="2"), target())
        self.assertEqual(preview.record(bucket, intent(), result(intent()))["revision"], 1)

    def test_degraded_keeps_previous_verified_deployment(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        first = preview.record(bucket, intent(), result(intent()))
        request = intent(revision=1, identity="2")
        preview.reserve(bucket, request, target())
        value = result(request, "degraded")
        value["content"]["state"] = "unverified"
        value["failure"] = "latest byte readback mismatch"
        value["provider"]["deployment"]["id"] = "new-deployment"
        updated = preview.record(bucket, request, value)
        self.assertEqual(updated["last_verified"], first["last_verified"])
        self.assertNotEqual(updated["actual"], updated["last_verified"])

    def test_tombstone_rejects_old_apply_and_allows_explicit_recreation(self):
        bucket = Store()
        request = intent("discard")
        preview.reserve(bucket, request, target())
        current = preview.record(bucket, request, discarded(request))
        self.assertEqual((current["phase"], current["revision"], current["actual"]), ("discarded", 1, None))
        with self.assertRaises(Refusal):
            preview.reserve(bucket, intent(identity="2"), target())
        self.assertEqual(preview.reserve(bucket, intent(revision=1, identity="2"), target())["phase"], "active")

    def test_same_request_identity_cannot_change_source_or_revision(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        preview.record(bucket, intent(), result(intent()))
        request = intent(revision=1)
        with self.assertRaises(Refusal):
            preview.reserve(bucket, request, target())
        self.assertEqual(preview.read(bucket, intent())[0]["phase"], "verified")

    def test_lost_reservation_reply_can_be_read_back(self):
        bucket = Lost()
        with self.assertRaises(OSError):
            preview.reserve(bucket, intent(), target())
        self.assertEqual(preview.reserve(bucket, intent(), target())["active"], intent())

    def test_lost_completion_reply_can_be_read_back(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        lost = Lost()
        lost.objects = bucket.objects
        with self.assertRaises(OSError):
            preview.record(lost, intent(), result(intent()))
        self.assertEqual(preview.record(lost, intent(), result(intent()))["phase"], "verified")

    def test_competing_snapshot_cannot_overwrite_reservation(self):
        bucket = Store()
        _, etag = preview.read(bucket, intent())
        preview.reserve(bucket, intent(), target())
        competitor = preview.started(preview.initial(intent(identity="2")), intent(identity="2"))
        with self.assertRaises(Conflict):
            bucket.swap(f"{preview.location(intent())}/current.json", canonical.encode(competitor), etag)
        self.assertEqual(preview.read(bucket, intent())[0]["active"], intent())

    def test_wrong_registration_and_result_never_write(self):
        bucket = Store()
        with self.assertRaises(Refusal):
            preview.reserve(bucket, intent(), dict(target(), generation="2" * 32))
        self.assertEqual(bucket.writes, [])
        preview.reserve(bucket, intent(), target())
        count = len(bucket.writes)
        with self.assertRaises(Refusal):
            preview.record(bucket, intent(identity="2"), result(intent(identity="2")))
        self.assertEqual(len(bucket.writes), count)

    def test_malformed_current_fails_closed(self):
        bucket = Store()
        current = preview.reserve(bucket, intent(), target())
        for key, value in (("schema", "unknown"), ("revision", True), ("active", None), ("environment", {}), ("registration", "2" * 64)):
            broken = copy.deepcopy(current)
            broken[key] = value
            bucket.put(f"{preview.location(intent())}/current.json", canonical.encode(broken))
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.read(bucket, intent())

    def test_settled_history_cannot_be_rewritten_as_ready(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        current = preview.record(bucket, intent(), result(intent()))
        for key, value in (("phase", "discarded"), ("last", None), ("actual", None), ("last_verified", None), ("revision", 8)):
            broken = copy.deepcopy(current)
            broken[key] = value
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.checked(broken, intent())

    def test_confirmed_prewrite_failure_advances_without_losing_old_content(self):
        bucket = Store()
        preview.reserve(bucket, intent(), target())
        first = preview.record(bucket, intent(), result(intent()))
        request = intent(revision=1, identity="2")
        preview.reserve(bucket, request, target())
        value = unknown(request)
        value.update(outcome="failed", failure="build refused before upload")
        value["provider"].update(state="unchanged", quiescent=True)
        current = preview.record(bucket, request, value)
        self.assertEqual((current["revision"], current["active"]), (2, None))
        self.assertEqual(current["actual"], first["actual"])
        self.assertEqual(current["last_verified"], first["last_verified"])

    def test_intent_created_before_failed_cas_is_not_permission(self):
        bucket = Store()
        request = intent()
        preview.immutable(bucket, f"{preview.location(request)}/requests/{request['request']}/intent.json", request)
        self.assertEqual(preview.read(bucket, request)[0]["active"], None)
        with self.assertRaises(Refusal):
            preview.record(bucket, request, result(request))
        self.assertEqual(preview.reserve(bucket, request, target())["active"], request)

    def test_immutable_result_without_current_update_can_be_resumed(self):
        bucket = Store()
        request = intent()
        preview.reserve(bucket, request, target())
        value = result(request)
        key = f"{preview.location(request)}/requests/{request['request']}/{canonical.digest(value)}.json"
        preview.immutable(bucket, key, value)
        self.assertEqual(preview.read(bucket, request)[0]["phase"], "active")
        self.assertEqual(preview.record(bucket, request, value)["phase"], "verified")

    def test_unknown_discard_cannot_create_a_tombstone(self):
        bucket = Store()
        request = intent("discard")
        preview.reserve(bucket, request, target())
        current = preview.record(bucket, request, unknown(request))
        self.assertEqual((current["revision"], current["phase"]), (0, "unknown"))
        with self.assertRaises(Refusal):
            preview.reserve(bucket, intent(identity="2"), target())

    def test_invalid_duplicate_and_oversized_json_refuse(self):
        bucket = Store()
        for body in (b"invalid", b'{"schema":"a","schema":"b"}', b" " * 65537):
            bucket.put(f"{preview.location(intent())}/current.json", body)
            with self.subTest(body=body[:30]), self.assertRaises(Refusal):
                preview.read(bucket, intent())


class Snapshots(unittest.TestCase):
    def test_body_and_etag_come_from_one_get(self):
        bucket = object.__new__(Bucket)
        calls = []

        def respond(operation):
            calls.append(operation.method)
            return 200, b"body", {"etag": "exact-version"}

        bucket.request = respond
        self.assertEqual(bucket.snapshot("key"), (b"body", "exact-version"))
        self.assertEqual(calls, ["GET"])

    def test_only_explicit_404_is_absent(self):
        bucket = object.__new__(Bucket)
        bucket.request = lambda operation: (404, b"", {})
        self.assertEqual(bucket.snapshot("key"), (None, None))
        for status in (200, 403, 500):
            bucket.request = lambda operation: (status, b"", {})
            with self.subTest(status=status), self.assertRaises(Refusal):
                bucket.snapshot("key")


if __name__ == "__main__":
    unittest.main()
