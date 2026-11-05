import copy
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind import publication
from lib.refusal import Refusal
from lib.store import plan
from scripts import channel
from tests.scripts.test_channel.world import World


class Publication(unittest.TestCase):
    def test_completion_requires_revalidation_before_any_channel_write(self):
        world = World(self)
        world.context["completion"] = {"schema": "wharf.release.completion/v1"}
        world.save()
        with self.assertRaisesRegex(Refusal, "revalidation"):
            publication.read(world.held)
        with mock.patch.object(channel.completion, "validate", side_effect=Refusal("completion changed")) as validate:
            self.rejected(world)
        validate.assert_called_once_with(world.workload, dict(world.context, source=str(world.root)), world.context["completion"])

    def test_successful_completion_revalidation_precedes_pointer_publication(self):
        world = World(self)
        world.context["completion"] = {"schema": "wharf.release.completion/v1"}
        world.save()
        def validate(bucket, context, checkpoint):
            self.assertEqual(world.bucket.writes, [])
            self.assertEqual(context["snapshot"], world.context["snapshot"])
        with mock.patch.object(channel.completion, "validate", side_effect=validate) as checked:
            self.assertEqual(channel.point(world.held)["pointer"]["state"], "advanced")
        checked.assert_called_once()

    def rejected(self, world):
        before = dict(world.bucket.objects)
        private = dict(world.workload.objects)
        with self.assertRaises(Refusal):
            channel.point(world.held)
        self.assertEqual(world.bucket.objects, before)
        self.assertEqual(world.bucket.writes, [])
        self.assertEqual(world.workload.objects, private)

    def test_complete_context_advances_pointer_and_managers(self):
        world = World(self)
        result = channel.point(world.held)
        self.assertEqual(result["pointer"]["state"], "advanced")
        self.assertEqual(result["pointer"]["managers"], ["manage.sh"])
        self.assertEqual(publication.read(world.held), world.source)

    def test_rerun_uses_successful_plan_from_failed_earlier_attempt(self):
        world = World(self, "2")
        world.runs["1"].update(status="completed", conclusion="failure")
        world.jobs["2"] = [world.jobs["2"][-1]]
        self.assertEqual(channel.point(world.held)["pointer"]["state"], "advanced")
        self.assertIn("actions/runs/7/attempts/1/jobs?per_page=100", world.calls)

    def test_wrong_native_run_attempt_source_and_producer_refuse(self):
        world = World(self)
        original = copy.deepcopy(world.runs["1"])
        for key, value in (("id", 8), ("run_attempt", 2), ("head_sha", "b" * 40), ("event", "pull_request"), ("head_branch", "other"), ("path", "other.yml"), ("status", "completed"), ("conclusion", "failure"), ("repository", {"full_name": "other/wharf"})):
            with self.subTest(key=key):
                world.runs["1"] = dict(original, **{key: value})
                self.rejected(world)

    def test_wrong_authority_and_unprotected_branch_refuse(self):
        world = World(self)
        with mock.patch.dict(world.repository, id=1):
            self.rejected(world)
        world.branch["protected"] = False
        self.rejected(world)

    def test_missing_inactive_ambiguous_and_foreign_channel_jobs_refuse(self):
        world = World(self)
        original = copy.deepcopy(world.jobs["1"])
        variants = [original[:1], original + [dict(original[-1], id=3)], original + [original[-1]]]
        variants += [original[:1] + [dict(original[-1], **{key: value})] for key, value in (("status", "completed"), ("conclusion", "failure"), ("run_id", 8), ("run_attempt", 2), ("head_sha", "b" * 40), ("head_branch", "other"), ("started_at", None))]
        for jobs in variants:
            with self.subTest(jobs=jobs):
                world.jobs["1"] = jobs
                self.rejected(world)

    def test_incomplete_job_inventory_and_wrong_runtime_role_refuse(self):
        world = World(self)
        with mock.patch.object(publication.control, "api", return_value={"jobs": [], "total_count": 1}):
            with self.assertRaises(Refusal):
                publication.jobs({"run": "7", "attempt": "1", "wharf": "a" * 40})
        with mock.patch.dict("os.environ", GITHUB_JOB="release"):
            self.rejected(world)

    def test_future_plan_and_unsuccessful_plan_or_record_refuse(self):
        world = World(self)
        world.held["planned"] = "2"
        self.rejected(world)
        world.held["planned"] = "1"
        original = copy.deepcopy(world.jobs["1"][0])
        for update in ({"conclusion": "failure"}, {"status": "in_progress"}, {"steps": []}, {"steps": [dict(original["steps"][0], conclusion="skipped")]}, {"steps": original["steps"] * 2}):
            with self.subTest(update=update):
                world.jobs["1"][0] = dict(original, **update)
                self.rejected(world)

    def test_plan_context_snapshot_and_decision_must_agree(self):
        world = World(self)
        original = copy.deepcopy(world.document)
        for field in ("run", "attempt", "wharf", "repository", "marker", "tree"):
            with self.subTest(field=field):
                world.document = copy.deepcopy(original)
                world.document["context"][field] = "different"
                world.save()
                self.rejected(world)
        world.document = copy.deepcopy(original)
        world.document["entries"]["channel"]["decision"] = "skip"
        world.save()
        self.rejected(world)

    def test_missing_and_conflicting_reservations_refuse_without_repair(self):
        world = World(self)
        key = plan.publication(world.context)
        del world.workload.objects[key]
        self.rejected(world)
        world.workload.objects[key] = canonical.encode({})
        self.rejected(world)

    def test_dirty_tracked_untracked_and_changed_head_refuse(self):
        world = World(self)
        path = world.root / "plumb.toml"
        content = path.read_text()
        path.write_text(content + "\n")
        self.rejected(world)
        path.write_text(content)
        extra = world.root / "extra"
        extra.write_text("new")
        self.rejected(world)
        from lib.process import git
        git(world.root, "add", ".")
        git(world.root, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "different")
        self.rejected(world)

    def test_seal_identity_mismatch_refuses_before_all_publication_writes(self):
        world = World(self)
        original = dict(world.seal)
        for key, value in (("schema", True), ("product", "other"), ("channel", "rc"), ("releaseVersion", "v1.2.4"), ("commit", "0" * 40)):
            with self.subTest(key=key):
                world.seal = dict(original, **{key: value})
                world.save_seal()
                self.rejected(world)

    def test_missing_plan_and_malformed_plan_refuse(self):
        world = World(self)
        key = plan.location(world.context)
        del world.workload.objects[key]
        self.rejected(world)
        for raw in (b"null", b"[]", b"{", canonical.encode(dict(world.document, schema=True))):
            with self.subTest(raw=raw):
                world.workload.objects[key] = raw
                self.rejected(world)

    def test_frozen_snapshot_source_mismatch_refuses(self):
        world = World(self)
        world.context["snapshot"]["source"] = dict(world.source, commit="0" * 40)
        world.save()
        self.rejected(world)
