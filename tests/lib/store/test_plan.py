import json
import unittest
from pathlib import Path
from unittest import mock

from lib.identity.guard import snapshot
from lib.refusal import Refusal
from lib.store import plan
from tests.lib.store.memory import Memory

CONTEXT = {
    "repository": "PerishLab/plumb",
    "marker": "v0.38.3",
    "commit": "a" * 40,
    "tree": "b" * 40,
    "wharf": "c" * 40,
    "run": "35493492267",
    "attempt": "1",
}
ENTRIES = {"binary-linux": {"key": "d" * 64, "decision": "run"}, "npm": {"decision": "skip"}}
IDENTITY = {"repository": "PerishLab/example", "marker": "v1.0.0", "commit": "a" * 40, "tree": "b" * 40}
RESULT = {"tree": "c" * 40, "packages": []}
SNAPSHOT = {"schema": "wharf.release.snapshot/v1", "source": {field: CONTEXT[field] for field in ("repository", "marker", "commit", "tree")}, "head": "e" * 40, "tree": "f" * 40, "packages": [], "controls": {}, "domain": {}, "guard": {}}


class Publication(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.document = {"context": dict(CONTEXT, snapshot=SNAPSHOT)}

    def test_the_first_combination_is_create_only_and_reusable(self):
        plan.reserve(self.bucket, self.document)
        original = self.bucket.get(plan.publication(CONTEXT))
        plan.reserve(self.bucket, self.document)
        self.assertEqual(self.bucket.get(plan.publication(CONTEXT)), original)

    def test_a_changed_package_set_refuses_without_replacing_the_record(self):
        plan.reserve(self.bucket, self.document)
        original = self.bucket.get(plan.publication(CONTEXT))
        changed = dict(SNAPSHOT, packages=[{"ecosystem": "cargo", "name": "plumb", "version": "changed"}])
        with self.assertRaisesRegex(Refusal, "another verified combination"):
            plan.reserve(self.bucket, {"context": dict(CONTEXT, snapshot=changed)})
        self.assertEqual(self.bucket.get(plan.publication(CONTEXT)), original)

    def test_another_release_identity_writes_nothing(self):
        with self.assertRaisesRegex(Refusal, "release identity"):
            plan.reserve(self.bucket, {"context": dict(CONTEXT, marker="v9.0.0", snapshot=SNAPSHOT)})
        self.assertEqual(self.bucket.writes, [])

    def test_partial_historical_publication_requires_review(self):
        observed = {"npm": {"decision": "run", "presence": "present", "existing": "true"}}
        with self.assertRaisesRegex(Refusal, "explicit recovery review"):
            plan.legacy(self.bucket, CONTEXT, {"npm": {"decision": "run"}}, observed)
        self.assertEqual(self.bucket.writes, [])

    def test_registered_recovery_keeps_the_same_combination(self):
        plan.reserve(self.bucket, self.document)
        plan.legacy(self.bucket, CONTEXT, {"npm": {"decision": "run"}}, {"npm": {"existing": "true"}})


class Reading(unittest.TestCase):
    def stored(self, entries):
        bucket = Memory()
        plan.record(bucket, CONTEXT, entries, {})
        return bucket

    def test_a_job_reads_the_plan_of_its_own_run(self):
        document = plan.read(self.stored(ENTRIES), CONTEXT)
        self.assertEqual(document["entries"]["binary-linux"]["key"], "d" * 64)

    def test_the_product_name_comes_from_the_repository_the_plan_names(self):
        self.assertEqual(plan.product(CONTEXT), "plumb")

    def test_a_document_of_another_schema_is_refused(self):
        bucket = self.stored(ENTRIES)
        bucket.put(plan.location(CONTEXT), json.dumps({"schema": 2, "entries": {}}).encode())
        with self.assertRaisesRegex(Refusal, "not a schema 1 plan"):
            plan.read(bucket, CONTEXT)

    def test_an_entry_the_plan_never_decided_refuses(self):
        with self.assertRaisesRegex(Refusal, "holds no entry binary-macos"):
            plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "binary-macos")

    def test_an_entry_the_plan_decided_to_skip_refuses_to_be_run(self):
        with self.assertRaisesRegex(Refusal, "decided 'skip' for npm"):
            plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "npm")

    def test_an_entry_the_plan_decided_to_run_is_handed_back(self):
        entry = plan.planned(plan.read(self.stored(ENTRIES), CONTEXT), "binary-linux")
        self.assertEqual(entry, {"key": "d" * 64, "decision": "run"})


class Address(unittest.TestCase):
    def test_a_run_owns_its_plan_and_the_marker_keeps_a_pointer(self):
        self.assertEqual(plan.location(CONTEXT), "plan/PerishLab/plumb/v0.38.3/35493492267-1.json")
        self.assertEqual(plan.standing(CONTEXT), "plan/PerishLab/plumb/v0.38.3/latest.json")

    def test_a_malformed_request_is_refused_before_anything_is_written(self):
        for field, value in (("repository", "plumb"), ("marker", "0.38.3")):
            with self.assertRaises(Refusal):
                plan.location(dict(CONTEXT, **{field: value}))


class Record(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def test_the_run_object_is_immutable_and_the_pointer_follows_it(self):
        held = plan.record(self.bucket, CONTEXT, ENTRIES, {"node": "24.18.0"})
        self.assertEqual((held["entries"], held["plan"]), (2, plan.location(CONTEXT)))
        self.assertEqual(self.bucket.get(plan.standing(CONTEXT)), self.bucket.get(plan.location(CONTEXT)))
        document = json.loads(self.bucket.get(plan.location(CONTEXT)))
        self.assertEqual(list(document["entries"]), ["binary-linux", "npm"])
        self.assertEqual(document["context"]["marker"], "v0.38.3")
        with self.assertRaises(Exception):
            plan.record(self.bucket, CONTEXT, ENTRIES, {"node": "24.18.0"})

    def test_a_second_run_of_one_marker_records_its_own_plan(self):
        plan.record(self.bucket, CONTEXT, ENTRIES, {})
        later = dict(CONTEXT, attempt="2")
        plan.record(self.bucket, later, {"npm": {"decision": "run"}}, {})
        self.assertNotEqual(self.bucket.get(plan.location(CONTEXT)), self.bucket.get(plan.location(later)))
        self.assertEqual(self.bucket.get(plan.standing(CONTEXT)), self.bucket.get(plan.location(later)))


class Shaped(unittest.TestCase):
    ENTRIES = {
        "binary-linux": {"key": "a" * 64, "decision": "run"},
        "dependencies-linux": {"key": "b" * 64, "decision": "skip"},
        "bind-linux": {"key": "c" * 64, "decision": "run", "consumes": ["binary-linux"]},
        "smoke-linux": {"key": "d" * 64, "decision": "run", "consumes": ["bind-linux"]},
        "suite-linux": {"key": "e" * 64, "decision": "skip"},
        "cfworker": {"key": "f" * 64, "decision": "run"},
        "npm": {"decision": "run"},
        "release": {"decision": "run"},
    }

    def test_an_entry_sits_one_layer_after_the_deepest_entry_it_consumes(self):
        self.assertEqual(
            plan.layered(self.ENTRIES)["layers"],
            [["binary-linux", "dependencies-linux", "suite-linux"], ["bind-linux"], ["smoke-linux"]],
        )

    def test_what_publishes_comes_last_whether_or_not_it_holds_a_key(self):
        self.assertEqual(plan.layered(self.ENTRIES)["last"], ["cfworker", "npm", "release"])

    def test_a_plan_with_nothing_to_build_has_no_layers(self):
        self.assertEqual(plan.layered({"npm": {"decision": "skip"}}), {"layers": [], "last": ["npm"]})

    def test_the_recorded_plan_carries_its_shape(self):
        bucket = Memory()
        plan.record(bucket, CONTEXT, self.ENTRIES, {})
        self.assertEqual(plan.read(bucket, CONTEXT)["shape"], plan.layered(self.ENTRIES))


class Recalled(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.context = dict(CONTEXT, marker="v0.38.3-rc.1", snapshot=dict(SNAPSHOT, source=dict(SNAPSHOT["source"], marker="v0.38.3-rc.1"), guard={"proved": "rc"}))
        self.observed = {field: value for field, value in SNAPSHOT.items() if field != "guard"}

    def test_a_later_marker_of_the_same_commit_recalls_the_verified_guard(self):
        self.assertTrue(plan.remember(self.bucket, self.context))
        recalled = {}
        self.assertEqual(plan.recall(self.bucket, self.observed, recalled), {"proved": "rc"})
        self.assertEqual(recalled, {"marker": "v0.38.3-rc.1", "run": CONTEXT["run"], "attempt": "1"})

    def test_another_combination_or_commit_recalls_nothing(self):
        plan.remember(self.bucket, self.context)
        changed = dict(self.observed, packages=[{"ecosystem": "cargo", "name": "plumb", "version": "changed"}])
        moved = dict(self.observed, source=dict(self.observed["source"], commit="9" * 40))
        for observed in (changed, moved):
            recalled = {}
            self.assertIsNone(plan.recall(self.bucket, observed, recalled))
            self.assertEqual(recalled, {})

    def test_the_first_verified_record_wins(self):
        self.assertTrue(plan.remember(self.bucket, self.context))
        later = dict(self.context, run="99", snapshot=dict(self.context["snapshot"], guard={"proved": "later"}))
        self.assertFalse(plan.remember(self.bucket, later))
        self.assertEqual(plan.recall(self.bucket, self.observed, {}), {"proved": "rc"})

    def test_a_record_filed_under_another_combination_refuses(self):
        name = plan.verification(dict(self.observed, guard={}))
        forged = dict(self.context["snapshot"], packages=[{"ecosystem": "cargo", "name": "plumb", "version": "forged"}])
        for body in ({"snapshot": forged, "run": "1", "attempt": "1"}, {"snapshot": self.context["snapshot"], "run": "1"}):
            with self.subTest(body=sorted(body)):
                self.bucket.objects[name] = json.dumps(body).encode()
                with self.assertRaisesRegex(Refusal, "does not hold the verified combination"):
                    plan.recall(self.bucket, self.observed, {})


class Receipt(unittest.TestCase):
    def receipt(self, recall, expected=None):
        held = {"request": snapshot.Request(Path("/source"), IDENTITY, {"rust.version": "1"}, expected, recall=recall)}
        return snapshot.receipt(held, "d" * 40, RESULT, {"plumb": "v1"})

    def test_a_recalled_guard_is_rechecked_and_runs_no_guard(self):
        observed = []
        def recall(held):
            observed.append(held)
            return {"proved": "rc"}
        with mock.patch.object(snapshot, "proof") as proof, mock.patch.object(snapshot, "verified") as verified:
            receipt = self.receipt(recall)
        verified.assert_not_called()
        self.assertEqual(json.loads(proof.call_args.args[3]), {"proved": "rc"})
        self.assertEqual(receipt["guard"], {"proved": "rc"})
        self.assertEqual(observed[0], {field: value for field, value in receipt.items() if field != "guard"})
        self.assertEqual(receipt["source"]["marker"], "v1.0.0")

    def test_nothing_recalled_runs_the_actual_guard(self):
        with mock.patch.object(snapshot, "verified", return_value={"proved": "now"}) as verified:
            self.assertEqual(self.receipt(lambda held: None)["guard"], {"proved": "now"})
        verified.assert_called_once()

    def test_a_recalled_guard_the_running_authority_rejects_refuses(self):
        with mock.patch.object(snapshot, "proof", side_effect=Refusal("Guard differs")), mock.patch.object(snapshot, "verified") as verified:
            with self.assertRaisesRegex(Refusal, "Guard differs"):
                self.receipt(lambda held: {"proved": "foreign"})
        verified.assert_not_called()

    def test_a_planned_snapshot_never_consults_recall(self):
        recall = mock.Mock()
        expected = {"schema": "wharf.release.snapshot/v1", "source": IDENTITY, "head": "d" * 40, "tree": "c" * 40, "packages": [], "controls": {"plumb": "v1"}, "domain": {"rust.version": "1"}, "guard": {"proved": "plan"}}
        with mock.patch.object(snapshot, "proof"):
            self.assertEqual(self.receipt(recall, expected), expected)
        recall.assert_not_called()
