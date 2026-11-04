import json
import unittest
from unittest import mock

from lib.content import canonical
from lib.refusal import Refusal
from lib.store import distribution, plan
from scripts import distribution as recording
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/plumb", "marker": "v0.42.0-rc.1", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1"}
DECIDED = {"release": "run", "npm": "run", "oci": "skip", "chart": "skip", "cargo": "run", "cfworker": "skip", "channel": "run"}
KEY = "v1/releases/rc/v0.42.0-rc.1/distribution.json"


PRESENCE = {"release": "present", "npm": "present", "oci": "none", "chart": "none", "cargo": "present", "cfworker": "present", "channel": "present"}
SNAPSHOT = {"schema": "wharf.release.snapshot/v1", "source": {field: CONTEXT[field] for field in distribution.IDENTITY}, "head": "e" * 40, "tree": "f" * 40, "packages": [{"ecosystem": "cargo", "name": "plumb", "version": "0.76.2"}], "controls": {}, "domain": {}}


def needs(**results):
    held = {"plan": {"result": "success", "outputs": dict(DECIDED, planned="1", presence=json.dumps(PRESENCE))}, "layer-1": {"result": "success"}}
    for job, decision in DECIDED.items():
        held[job] = {"result": results.get(job, "success" if decision == "run" else "skipped")}
        if job != "channel" and held[job]["result"] == "success":
            held[job]["outputs"] = {"snapshot": json.dumps(SNAPSHOT)}
    return held


class Record(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def record(self, held, **context):
        return distribution.record(self.bucket, dict(CONTEXT, **context), held, self.bucket.get)

    def standing(self):
        return json.loads(self.bucket.get(KEY))

    def test_a_marker_distributes_to_its_own_channel_path(self):
        self.assertEqual(distribution.location("v0.42.0"), "v1/releases/stable/v0.42.0/distribution.json")
        self.assertEqual(self.record(needs())["distribution"], KEY)

    def test_each_medium_records_what_became_of_it(self):
        self.record(needs(npm="failure", channel="skipped"))
        media = self.standing()["media"]
        self.assertEqual(media, {"binaries": "published", "npm": "failed", "oci": "none", "chart": "none", "cargo": "published", "cfworker": "present", "channel": "unreached"})

    def test_a_plan_that_told_no_presence_leaves_skipped_until_one_does(self):
        bare = needs()
        bare["plan"]["outputs"] = DECIDED
        self.record(bare)
        self.assertEqual(self.standing()["media"]["oci"], "skipped")
        self.record(needs(), run="8")
        self.assertEqual(self.standing()["media"]["oci"], "none")

    def test_a_run_whose_jobs_all_settled_is_complete(self):
        self.record(needs())
        held = self.standing()
        self.assertEqual((held["state"], held["completed"]["run"], held["commit"]), ("complete", "7", "a" * 40))

    def test_a_failed_job_leaves_the_marker_incomplete(self):
        self.record(needs(cargo="failure"))
        self.assertEqual((self.standing()["state"], self.standing()["completed"]), ("incomplete", None))

    def test_a_later_attempt_completes_what_an_earlier_one_left(self):
        self.record(needs(npm="failure", release="skipped", channel="skipped"))
        self.record(needs(release="skipped"), attempt="2")
        held = self.standing()
        self.assertEqual((held["state"], held["media"]["npm"], held["media"]["channel"]), ("complete", "published", "pointed"))
        self.assertEqual(held["completed"]["attempt"], "2")

    def test_retained_planner_reference_stays_paired_with_first_completion(self):
        reference = plan.reference(plan.document(CONTEXT, {}, {}))
        held = needs()
        held["plan"]["outputs"]["plan-reference"] = json.dumps(reference)
        self.record(held, attempt="2")
        self.assertEqual(self.standing()["plan"], reference)
        fresh = needs()
        fresh["plan"]["outputs"]["plan-reference"] = json.dumps(plan.reference(plan.document(dict(CONTEXT, run="8"), {}, {})))
        self.record(fresh, run="8")
        self.assertEqual(self.standing()["completed"]["attempt"], "2")
        self.assertEqual(self.standing()["plan"], reference)
        self.assertEqual(self.standing()["attempt"]["run"], "8")

    def test_later_plan_never_attaches_to_a_legacy_completion(self):
        self.record(needs())
        fresh = needs()
        fresh["plan"]["outputs"]["plan-reference"] = json.dumps(plan.reference(plan.document(dict(CONTEXT, run="8"), {}, {})))
        self.record(fresh, run="8")
        self.assertNotIn("plan", self.standing())
        self.assertEqual(self.standing()["completed"]["run"], "7")

    def test_foreign_planner_reference_refuses_before_distribution_write(self):
        held = needs()
        held["plan"]["outputs"]["plan-reference"] = json.dumps(plan.reference(plan.document(dict(CONTEXT, commit="d" * 40), {}, {})))
        with self.assertRaisesRegex(Refusal, "another source"):
            self.record(held)
        self.assertFalse(self.bucket.writes)

    def test_a_reference_to_a_different_successful_attempt_cannot_become_completion(self):
        held = needs()
        held["plan"]["outputs"].update(planned="2", **{"plan-reference": json.dumps(plan.reference(plan.document(CONTEXT, {}, {})))})
        with self.assertRaisesRegex(Refusal, "actual planner attempt"):
            self.record(held, attempt="2")
        self.assertFalse(self.bucket.writes)

    def test_nothing_published_or_complete_is_taken_back(self):
        self.record(needs())
        self.record(needs(npm="failure", channel="cancelled"), run="8")
        held = self.standing()
        self.assertEqual((held["state"], held["completed"]["run"], held["attempt"]["run"]), ("complete", "7", "8"))
        self.assertEqual((held["media"]["npm"], held["media"]["channel"]), ("published", "pointed"))

    def test_a_marker_recorded_at_another_commit_refuses(self):
        self.record(needs())
        held = needs()
        for job in ("release", "npm", "cargo"):
            held[job]["outputs"] = {"snapshot": json.dumps(dict(SNAPSHOT, source=dict(SNAPSHOT["source"], commit="d" * 40)))}
        with self.assertRaisesRegex(Refusal, "never moves"):
            self.record(held, commit="d" * 40)

    def test_a_record_not_served_as_written_refuses(self):
        with self.assertRaisesRegex(Refusal, "not served"):
            distribution.record(self.bucket, CONTEXT, needs(), lambda key: b"{}")

    def test_only_a_successful_publisher_records_its_actual_combination(self):
        held = needs(cargo="failure")
        for name in ("release", "cargo"):
            held[name]["outputs"] = {"snapshot": json.dumps(SNAPSHOT)}
        self.record(held)
        self.assertEqual(self.standing()["resolution"], {"binaries": SNAPSHOT, "npm": SNAPSHOT})
        self.assertNotIn("cargo", self.standing()["resolution"])

    def test_recovery_preserves_the_original_published_combination(self):
        held = needs(npm="failure")
        held["release"]["outputs"] = {"snapshot": json.dumps(SNAPSHOT)}
        self.record(held)
        later = needs()
        later["npm"]["outputs"] = {"snapshot": json.dumps(SNAPSHOT)}
        self.record(later, attempt="2")
        self.assertEqual(self.standing()["resolution"]["binaries"], SNAPSHOT)

    def test_a_changed_published_combination_preserves_the_original_record(self):
        held = needs(npm="failure")
        held["release"]["outputs"] = {"snapshot": json.dumps(SNAPSHOT)}
        self.record(held)
        original = self.bucket.get(KEY)
        later = needs()
        later["npm"]["outputs"] = {"snapshot": json.dumps(dict(SNAPSHOT, packages=[]))}
        with self.assertRaisesRegex(Refusal, "different published combinations"):
            self.record(later, attempt="2")
        self.assertEqual(self.bucket.get(KEY), original)

    def test_a_historical_registry_presence_gains_no_current_version_claim(self):
        historical = distribution.observed(CONTEXT, needs())
        historical.pop("resolution")
        self.bucket.put(KEY, json.dumps(historical).encode())
        self.record(needs(**{job: "skipped" for job in DECIDED}), attempt="2")
        self.assertNotIn("resolution", self.standing())

    def test_a_successful_publisher_without_snapshot_writes_nothing(self):
        for job in ("release", "npm", "oci", "chart", "cargo", "cfworker"):
            held = needs(**{job: "success"})
            held[job].pop("outputs")
            with self.subTest(job=job), self.assertRaisesRegex(Refusal, "no verified snapshot"):
                self.record(held)
            self.assertEqual(self.bucket.writes, [])

    def test_invalid_successful_snapshots_preserve_the_existing_record(self):
        self.record(needs())
        original = self.bucket.get(KEY)
        writes = list(self.bucket.writes)
        for snapshot in ("", "{", "null", "[]", "{}"):
            held = needs()
            held["npm"]["outputs"] = {"snapshot": snapshot}
            with self.subTest(snapshot=snapshot), self.assertRaises(Refusal):
                self.record(held, attempt="2")
            self.assertEqual(self.bucket.get(KEY), original)
            self.assertEqual(self.bucket.writes, writes)

    def test_a_publisher_reporting_another_release_writes_nothing(self):
        held = needs()
        held["npm"]["outputs"] = {"snapshot": json.dumps(dict(SNAPSHOT, source=dict(SNAPSHOT["source"], marker="v9.0.0")))}
        with self.assertRaisesRegex(Refusal, "release identity"):
            self.record(held)
        self.assertEqual(self.bucket.writes, [])


class Publication(unittest.TestCase):
    def setUp(self):
        self.blob = Memory()
        self.public = Memory()
        self.document = plan.document(CONTEXT, {}, {})
        self.reference = plan.reference(self.document)
        self.blob.objects[plan.location(CONTEXT)] = canonical.encode(self.document)

    def held(self, reference):
        observed = needs()
        observed["plan"]["outputs"]["plan-reference"] = json.dumps(reference)
        return dict(CONTEXT, attempt="2", trial="null", needs=json.dumps(observed))

    def test_real_plan_bytes_are_verified_before_the_release_writer_is_opened(self):
        with mock.patch.object(recording.r2, "configured", return_value=self.blob), mock.patch.object(recording.r2, "writer", return_value=self.public), mock.patch.object(recording.release, "fetch", side_effect=lambda url: self.public.get(KEY)), mock.patch.object(recording.parameters, "answer"):
            self.assertEqual(recording.record(self.held(self.reference))["state"], "complete")
        self.assertEqual(json.loads(self.public.get(KEY))["plan"], self.reference)
        self.assertFalse(self.blob.writes)

    def test_altered_unavailable_or_foreign_plan_never_opens_the_release_writer(self):
        for change in ({"sha256": "f" * 64}, {"commit": "f" * 40}, {"run": "8"}):
            with self.subTest(change=change), mock.patch.object(recording.r2, "configured", return_value=self.blob), mock.patch.object(recording.r2, "writer") as writer:
                with self.assertRaises(Refusal):
                    recording.record(self.held(dict(self.reference, **change)))
                writer.assert_not_called()
        self.blob.objects.clear()
        with mock.patch.object(recording.r2, "configured", return_value=self.blob), mock.patch.object(recording.r2, "writer") as writer:
            with self.assertRaises(Refusal):
                recording.record(self.held(self.reference))
            writer.assert_not_called()
