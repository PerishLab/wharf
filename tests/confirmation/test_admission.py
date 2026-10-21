import copy
import unittest
from unittest import mock

from lib.content.lane import codec, shape
from lib.process.confirmation import admission, authority, history, policy
from lib.process.lane import execution
from lib.refusal import Refusal
from lib.store.lane import documents, public, state
from tests.confirmation.admitted import confirmed


class Admission(unittest.TestCase):
    def test_exact_independent_observer_and_original_facts_are_detached_read_only(self):
        intent, pins, readers, trusted, body = confirmed()
        saved = copy.deepcopy(pins)
        with mock.patch.object(public.Reader, "read", return_value=body) as read:
            result = admission.admit(intent, pins, readers, trusted)
        self.assertEqual(result["schema"], "wharf.lane.confirmation-admission/v1")
        self.assertEqual(set(result), {"schema", "selector", "pins", "retained", "authority"})
        self.assertEqual(result["pins"], policy.bound(pins))
        self.assertEqual(result["retained"]["current"]["phase"], "reserved")
        self.assertIsNone(result["retained"]["current"]["last"])
        self.assertEqual(result["authority"]["workflow"]["run"], 456)
        self.assertEqual(result["authority"]["actors"]["actor"]["id"], 30)
        self.assertEqual(result["authority"]["original"]["workflow"]["actors"]["actor"]["id"], 20)
        self.assertEqual(result["authority"]["source"], {"commit": history.COMMIT, "tree": history.TREE, "graph": "wharf.lane.original-graph/v1"})
        self.assertEqual(len(result["authority"]["original"]["jobs"]), 9)
        self.assertEqual(len(readers[0].calls), 12)
        self.assertEqual(read.call_count, 3)
        self.assertTrue(all(call[0] in ("permission", "repository") for call in trusted[2].calls))
        self.assertFalse(hasattr(readers[0], "create"))
        self.assertFalse(hasattr(readers[0], "swap"))
        result["retained"]["current"]["phase"] = "settled"
        result["authority"]["actors"]["actor"]["id"] = 20
        self.assertEqual(pins, saved)
        self.assertEqual(readers[0].bucket.current["phase"], "reserved")
        self.assertNotIn("writer", result)
        self.assertNotIn("permit", result)

    def test_real_unknown_phase_is_preserved_without_rewriting_history(self):
        intent, pins, readers, trusted, body = confirmed()
        bucket = readers[0].bucket
        operation = codec.load("record", body)["entry"]["value"]
        bucket.current = state.finished(bucket.current, operation, bucket.target)
        pins["current"]["digest"] = shape.digest(bucket.current)
        with mock.patch.object(public.Reader, "read", return_value=body):
            result = admission.admit(intent, pins, readers, trusted)
        self.assertEqual(result["retained"]["current"], bucket.current)
        self.assertEqual(result["retained"]["current"]["phase"], "unknown")

    def test_current_bytes_etag_registration_generation_and_intent_movement_refuse(self):
        for fault in ("etag", "digest", "disabled", "generation", "registration", "intent", "missing", "custody"):
            intent, pins, readers, trusted, body = confirmed()
            store = readers[0]
            if fault == "etag":
                store.etags["current"] = '"moved"'
            elif fault == "digest":
                pins["current"]["digest"] = "0" * 64
            elif fault == "disabled":
                store.bucket.target["enabled"] = False
            elif fault == "generation":
                store.bucket.target["generation"] = "0" * 64
            elif fault == "registration":
                store.bucket.target["mapping"]["account"] = "0" * 32
            elif fault == "intent":
                store.bucket.objects[store.keys["intent"]] = b"{}"
            elif fault == "missing":
                del store.bucket.objects[store.keys["custody"]]
            else:
                value = documents.decode(store.bucket.objects[store.keys["custody"]])
                store.bucket.objects[store.keys["custody"]] = documents.encode(dict(value, attempt=2))
            with self.subTest(fault=fault), mock.patch.object(public.Reader, "read", return_value=body) as read, self.assertRaises(Refusal):
                admission.admit(intent, pins, readers, trusted)
            self.assertFalse(trusted[1].calls)
            read.assert_not_called()

    def test_settled_current_is_never_reopened_even_with_fresh_matching_pins(self):
        intent, pins, readers, trusted, body = confirmed()
        operation = codec.load("record", body)["entry"]["value"]
        operation["outcome"] = {"outcome": "applied", "material": {"state": "present", "artifact": operation["request"]["intent"]["artifact"]}, "writer": "idle"}
        bucket = readers[0].bucket
        bucket.current = state.finished(bucket.current, operation, bucket.target)
        pins["current"]["digest"] = shape.digest(bucket.current)
        with mock.patch.object(public.Reader, "read", return_value=body) as read, self.assertRaises(Refusal):
            admission.admit(intent, pins, readers, trusted)
        read.assert_not_called()
        self.assertFalse(trusted[1].calls)

    def test_public_bytes_digest_kind_unknown_and_entire_assessment_must_match(self):
        for fault in ("corrupt", "noncanonical", "oversized", "applied", "assessment", "request"):
            intent, pins, readers, trusted, body = confirmed()
            record = codec.load("record", body)
            operation = record["entry"]["value"]
            if fault == "corrupt":
                body = b"{}"
            elif fault == "noncanonical":
                body += b"\n"
            elif fault == "oversized":
                body = b" " * 65537
            else:
                if fault == "applied":
                    operation["outcome"] = {"outcome": "applied", "material": {"state": "present", "artifact": operation["request"]["intent"]["artifact"]}, "writer": "idle"}
                elif fault == "assessment":
                    operation["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
                else:
                    operation["request"]["context"]["request"] = "0" * 64
                    operation["assessment"]["request"] = codec.digest("request", operation["request"])
                body = codec.encode("record", record)
                pins["original"] = readers[1].reference(codec.digest("record", record))
                intent["original"] = pins["original"]
            with self.subTest(fault=fault), mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
                admission.admit(intent, pins, readers, trusted)
            self.assertFalse(trusted[1].calls)

    def test_observer_run_actors_permissions_and_original_source_are_independent(self):
        for fault in ("original-run", "attempt", "head", "path", "status", "actor", "triggering_actor", "permission", "push", "source-tree"):
            intent, pins, readers, trusted, body = confirmed()
            context, control, product = trusted
            current = control.runs[456]
            if fault == "original-run":
                context = pins["context"]
            elif fault == "attempt":
                current["run_attempt"] = 2
            elif fault == "head":
                current["head_sha"] = history.COMMIT
            elif fault == "path":
                current["path"] = ".github/workflows/ship.yml"
            elif fault == "status":
                current["status"] = "completed"
            elif fault in ("actor", "triggering_actor"):
                current[fault] = copy.deepcopy(control.runs[123][fault])
            elif fault == "permission":
                product.permissions["dave"]["permission"] = "read"
            elif fault == "push":
                product.permissions["carol"]["user"]["permissions"]["push"] = False
            else:
                control.source["tree"]["sha"] = "0" * 40
            with self.subTest(fault=fault), mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
                admission.admit(intent, pins, readers, (context, control, product))

    def test_historical_graph_does_not_follow_new_producers_and_foreign_jobs_refuse(self):
        intent, pins, readers, trusted, body = confirmed()
        with mock.patch.object(execution, "producer_names", return_value={"[verify] future graph": "success"}), mock.patch.object(public.Reader, "read", return_value=body):
            result = admission.admit(intent, pins, readers, trusted)
        self.assertEqual({job["name"] for job in result["authority"]["original"]["jobs"]}, set(history.NAMES))
        trusted[1].inventory["jobs"][0]["name"] = "[verify] future graph"
        with mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
            admission.admit(intent, pins, readers, trusted)

    def test_all_protected_versions_are_revalidated_and_movement_stops(self):
        for target in ("registration", "current", "intent", "custody"):
            for after in (4, 8):
                intent, pins, readers, trusted, body = confirmed()
                snapshot = readers[0].snapshot

                def read(key):
                    if len(readers[0].calls) == after:
                        readers[0].etags[target] = '"moved"'
                    return snapshot(key)

                with self.subTest(target=target, after=after), mock.patch.object(readers[0], "snapshot", side_effect=read), mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
                    admission.admit(intent, pins, readers, trusted)

    def test_permission_run_attempt_and_original_movement_after_first_pass_refuse(self):
        for fault in ("permission", "observer", "original"):
            intent, pins, readers, trusted, body = confirmed()
            observe = authority.observe

            def changed(*args):
                result = observe(*args)
                if fault == "permission":
                    trusted[2].permissions["dave"]["permission"] = "read"
                else:
                    trusted[1].runs[456 if fault == "observer" else 123]["run_attempt"] = 2
                return result

            with self.subTest(fault=fault), mock.patch.object(authority, "observe", side_effect=changed), mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
                admission.admit(intent, pins, readers, trusted)

    def test_public_movement_failure_or_wrong_prepared_origin_has_no_fallback(self):
        for fault in ("move", "failure", "origin"):
            intent, pins, readers, trusted, body = confirmed()
            if fault == "origin":
                readers = (readers[0], public.prepare({"authority": "wharf", "origin": "https://foreign.example.test"}))
            effect = [body, b"{}"] if fault == "move" else Refusal("public unavailable")
            with self.subTest(fault=fault), mock.patch.object(public.Reader, "read", side_effect=effect), self.assertRaises(Refusal):
                admission.admit(intent, pins, readers, trusted)
            if fault == "origin":
                self.assertFalse(readers[0].calls)
                self.assertFalse(trusted[1].calls)


if __name__ == "__main__":
    unittest.main()
