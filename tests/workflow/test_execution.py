import copy
import unittest
from unittest import mock

from lib.content import canonical
from lib.content.lane import codec
from lib.process.lane import entry, execution, reservation
from lib.refusal import Refusal
from lib.store.lane import documents
from tests.lib.preview.test_admission import CONTEXT
from tests.workflow import fixtures
from tests.workflow.test_reservation import ARTIFACT


def prepared(operation="apply"):
    target = fixtures.registered()
    intent = fixtures.request(operation, target)
    store = fixtures.Store(target, fixtures.settled(target) if operation == "dispose" else None)
    observation = entry.admit(intent, store, CONTEXT, fixtures.provider())
    held = {"request": fixtures.encoded(intent), "artifact": fixtures.encoded(ARTIFACT if operation == "apply" else None)}
    with mock.patch.object(entry, "observe", return_value=observation), mock.patch.object(reservation.r2, "configured", return_value=store):
        result = reservation.prepare(held, fixtures.platform())
    return intent, store, result


class Execution(unittest.TestCase):
    def test_fresh_reserved_custody_is_independently_checked_without_writes(self):
        for operation in ("apply", "dispose", "inspect"):
            with self.subTest(operation=operation):
                intent, store, value = prepared(operation)
                store.calls.clear()
                result = execution.admit(intent, value, store, (CONTEXT, fixtures.provider()))
                self.assertEqual(result["prepared"], value)
                self.assertEqual(result["intent"], canonical.digest(intent))
                self.assertFalse(any(call[0] in ("create", "swap") for call in store.calls))
                self.assertEqual(sum(call[0] == "get" for call in store.calls), 0 if operation == "inspect" else 2)
                result["prepared"]["registration"]["enabled"] = False
                self.assertTrue(store.target["enabled"])

    def test_foreign_or_retried_run_attempt_and_missing_or_moved_custody_refuse(self):
        intent, store, value = prepared()
        key = execution.location(intent)
        for changed in (dict(CONTEXT, run=124), dict(CONTEXT, attempt=2), dict(CONTEXT, commit="0" * 40)):
            with self.subTest(context=changed), self.assertRaises(Refusal):
                execution.admit(intent, value, store, (changed, fixtures.provider()))
        for body in (b"{}", documents.encode(dict(value["custody"], attempt=2))):
            store.objects[key] = body
            with self.assertRaises(Refusal):
                execution.admit(intent, value, store, (CONTEXT, fixtures.provider()))

    def test_unknown_settled_disabled_changed_request_and_assessment_refuse(self):
        intent, store, value = prepared()
        changes = []
        changed = copy.deepcopy(value)
        changed["current"]["phase"] = "unknown"
        changes.append(changed)
        changed = copy.deepcopy(value)
        changed["registration"]["enabled"] = False
        changes.append(changed)
        changed = copy.deepcopy(value)
        changed["request"]["context"]["request"] = "0" * 64
        changes.append(changed)
        changed = copy.deepcopy(value)
        changed["assessment"]["conditions"]["writer"] = "unknown"
        changes.append(changed)
        for changed in changes:
            with self.assertRaises(Refusal):
                execution.admit(intent, changed, store, (CONTEXT, fixtures.provider()))
        store.target["generation"] = "0" * 64
        with self.assertRaises(Refusal):
            execution.admit(intent, value, store, (CONTEXT, fixtures.provider()))

    def test_permission_loss_cancellation_and_state_movement_during_authority_refuse(self):
        for failure in ("permission", "cancelled", "current", "registration", "custody"):
            intent, store, value = prepared()
            api = fixtures.provider()
            original = api.permission

            def moved(repository, login):
                result = original(repository, login)
                if failure == "permission":
                    result["permission"] = "read"
                elif failure == "current":
                    store.current = None
                elif failure == "registration":
                    store.target["enabled"] = False
                elif failure == "custody":
                    store.objects[execution.location(intent)] = b"{}"
                return result

            api.permission = moved
            if failure == "cancelled":
                api.observed["status"] = "completed"
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                execution.admit(intent, value, store, (CONTEXT, api))

    def test_execution_reader_adds_only_original_request_custody_key(self):
        intent = fixtures.request()
        env = {"WHARF_LANE_READER_" + key: value for key, value in {
            "ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "BUCKET": "lane-state",
            "ACCESS_KEY_ID": "read-id", "SECRET_ACCESS_KEY": "read-secret"}.items()}
        reader = execution.Reader(intent, env)
        self.assertIn(execution.location(intent), reader._keys)
        with self.assertRaises(Refusal):
            reader.get(execution.location(dict(intent, request="0" * 64)))
        reader = execution.Reader(fixtures.request("inspect"), env)
        self.assertEqual(len(reader._keys), 2)
        self.assertFalse(hasattr(reader, "create"))
        with mock.patch.object(reader, "snapshot", return_value=(None, None)), self.assertRaises(Refusal):
            reader.get("missing")


if __name__ == "__main__":
    unittest.main()
