import json
import unittest

from lib.refusal import Refusal
from lib.store import distribution
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/plumb", "marker": "v0.42.0-rc.1", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1"}
DECIDED = {"release": "run", "npm": "run", "oci": "skip", "chart": "skip", "cargo": "run", "cfworker": "skip", "channel": "run"}
KEY = "v1/releases/rc/v0.42.0-rc.1/distribution.json"


PRESENCE = {"release": "present", "npm": "present", "oci": "none", "chart": "none", "cargo": "present", "cfworker": "present", "channel": "present"}


def needs(**results):
    held = {"plan": {"result": "success", "outputs": dict(DECIDED, presence=json.dumps(PRESENCE))}, "layer-1": {"result": "success"}}
    for job, decision in DECIDED.items():
        held[job] = {"result": results.get(job, "success" if decision == "run" else "skipped")}
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

    def test_nothing_published_or_complete_is_taken_back(self):
        self.record(needs())
        self.record(needs(npm="failure", channel="cancelled"), run="8")
        held = self.standing()
        self.assertEqual((held["state"], held["completed"]["run"], held["attempt"]["run"]), ("complete", "7", "8"))
        self.assertEqual((held["media"]["npm"], held["media"]["channel"]), ("published", "pointed"))

    def test_a_marker_recorded_at_another_commit_refuses(self):
        self.record(needs())
        with self.assertRaisesRegex(Refusal, "never moves"):
            self.record(needs(), commit="d" * 40)

    def test_a_record_not_served_as_written_refuses(self):
        with self.assertRaisesRegex(Refusal, "not served"):
            distribution.record(self.bucket, CONTEXT, needs(), lambda key: b"{}")
