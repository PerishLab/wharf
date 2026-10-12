import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.lane import entry, reservation
from lib.refusal import Conflict, Refusal
from lib.store.lane import documents, state
from tests.lib.preview.test_admission import CONTEXT
from tests.workflow import fixtures


ARTIFACT = {"digest": "1" * 64, "source": {"repository": "PerishLab/crest", "commit": "d" * 40, "tree": "e" * 40},
            "build": {"inputs": "2" * 64, "world": "3" * 64}}


class Reservation(unittest.TestCase):
    def prepared(self, operation="apply"):
        target = fixtures.registered()
        requested = fixtures.request(operation, target)
        store = fixtures.Store(target, fixtures.settled(target) if operation == "dispose" else None)
        observed = entry.admit(requested, store, CONTEXT, fixtures.provider())
        held = {"request": fixtures.encoded(requested), "artifact": fixtures.encoded(ARTIFACT if operation == "apply" else None)}
        return store, observed, held

    @staticmethod
    def reserve(store, observation, held):
        with mock.patch.object(entry, "observe", return_value=observation):
            with mock.patch.object(reservation.r2, "configured", return_value=store):
                return reservation.prepare(held, fixtures.platform())

    def test_exact_apply_is_reserved_with_once_only_original_run_custody(self):
        store, observed, held = self.prepared()
        result = self.reserve(store, observed, held)
        self.assertEqual(result["current"]["phase"], "reserved")
        self.assertEqual(result["request"]["intent"], {"action": "deploy", "artifact": ARTIFACT})
        self.assertEqual(result["custody"]["request"], codec.digest("request", result["request"]))
        self.assertEqual((result["custody"]["run"], result["custody"]["attempt"]), (123, 1))
        self.assertIsNone(codec.admit(result["request"], result["assessment"], result["registration"]["declaration"]))
        key = documents.location(result["request"]["context"]["requested"]) + "/requests/" + "f" * 64 + "/custody.json"
        self.assertEqual(store.objects[key], documents.encode(result["custody"]))
        with self.assertRaises(Conflict):
            self.reserve(store, observed, held)
        self.assertEqual(store.current["active"], result["current"]["active"])

    def test_inspection_reserves_nothing_and_has_no_writer_custody(self):
        store, observed, held = self.prepared("inspect")
        result = self.reserve(store, observed, held)
        self.assertEqual(result["current"]["phase"], "vacant")
        self.assertIsNone(result["custody"])
        self.assertIsNone(store.current)
        self.assertFalse(store.objects)
        self.assertEqual(result["request"]["intent"], {"action": "inspect"})

    def test_disposal_binds_exact_present_precondition_without_build(self):
        store, observed, held = self.prepared("dispose")
        result = self.reserve(store, observed, held)
        self.assertEqual(result["request"]["intent"], {"action": "dispose"})
        self.assertEqual(result["current"]["observed"], {"state": "present", "revision": 1, "digest": "1" * 64})
        self.assertIsNotNone(result["custody"])

    def test_foreign_source_or_missing_build_and_non_apply_material_refuse(self):
        requested = fixtures.request()
        for changed in (dict(ARTIFACT, build=None), dict(ARTIFACT, source=dict(ARTIFACT["source"], tree="0" * 40))):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                reservation.requested(requested, changed)
        for operation in ("inspect", "dispose"):
            with self.assertRaises(Refusal):
                reservation.requested(fixtures.request(operation), ARTIFACT)

    def test_cas_failure_and_unknown_custody_completion_never_return_permit(self):
        store, observed, held = self.prepared()
        with mock.patch.object(store, "swap", side_effect=Conflict("moved")):
            with self.assertRaises(Conflict):
                self.reserve(store, observed, held)
        self.assertFalse(any(key.endswith("custody.json") for key in store.objects))
        store, observed, held = self.prepared()
        create = store.create

        def lost(key, body):
            create(key, body)
            if key.endswith("custody.json"):
                raise OSError("response lost")

        with mock.patch.object(store, "create", side_effect=lost), self.assertRaises(OSError):
            self.reserve(store, observed, held)
        self.assertEqual(store.current["phase"], "reserved")
        self.assertTrue(any(key.endswith("custody.json") for key in store.objects))
        with self.assertRaises(Conflict):
            self.reserve(store, observed, held)

    def test_busy_or_registration_movement_cannot_grant_a_new_writer(self):
        store, observed, held = self.prepared()
        result = self.reserve(store, observed, held)
        changed = copy.deepcopy(observed)
        changed["current"] = result["current"]
        with self.assertRaises(Refusal):
            reservation.assessment(result["request"], changed)
        store, observed, held = self.prepared()
        store.target["enabled"] = False
        with self.assertRaises(Refusal):
            self.reserve(store, observed, held)
        self.assertIsNone(store.current)


if __name__ == "__main__":
    unittest.main()
