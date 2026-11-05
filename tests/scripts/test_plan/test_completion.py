import copy
import json
import shutil
import unittest
from contextlib import ExitStack
from unittest import mock

from lib.content import canonical
from lib.identity.guard import snapshot
from lib.media import release
from lib.media.release import completion
from lib.process import git
from lib.refusal import Refusal
from lib.store import distribution, plan, workload
from scripts import plan as planner
from tests.lib.identity.test_guard import AUTHORITY, runtime
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory


class Completion(unittest.TestCase):
    def setUp(self):
        self.repository = Repository({"plumb.toml": '[release]\nbinaries=["demo"]\ntargets=["x86_64-unknown-linux-gnu"]\n'})
        self.addCleanup(shutil.rmtree, self.repository.root)
        root = self.repository.root
        self.identity = dict(repository="PerishLab/demo", marker="v1.2.3", commit=git(root, "rev-parse", "HEAD"), tree=git(root, "rev-parse", "HEAD^{tree}"))
        self.held = dict(self.identity, source=str(root), wharf="c" * 40, run="8", attempt="1", preparing=False)
        previous = dict(self.identity, wharf="d" * 40, run="7", attempt="1")
        proof = json.loads(runtime(commit=self.identity["commit"], tree=self.identity["tree"], root="/original", proof={"resolution": {"context": "ci-latest", "tree": self.identity["tree"], "packages": []}}))
        proof["guard"]["repository"] = self.identity["repository"]
        self.expected = dict(schema="wharf.release.snapshot/v1", source=self.identity, head=self.identity["commit"], tree=self.identity["tree"], packages=[], controls=dict(plumb="plumb v0.87.0", authority=AUTHORITY), domain={}, guard=proof)
        self.bucket = Memory()
        self.previous = dict(previous, snapshot=self.expected)
        self.key = "e" * 64
        self.entries = {"binary-linux": dict(key=self.key, decision="run"), "release": dict(decision="run", presence="present")}
        plan.record(self.bucket, self.previous, self.entries, {})
        plan.reserve(self.bucket, plan.document(self.previous, self.entries, {}))
        plan.remember(self.bucket, self.previous)
        self.bucket.create(workload.prefix(self.key) + "record.json", b"{}")
        self.placed = release.Release(**{name: previous[name] for name in ("repository", "marker", "commit", "wharf")})
        self.authority = release.place(self.placed)[2]
        self.public = {}
        artifact = self.object("demo-x86_64-unknown-linux-gnu.tar.gz", b"archive")
        manager = self.object("manage.sh", b"manager")
        self.seal = dict(schema=1, product="demo", releaseVersion="v1.2.3", channel="stable", commit=previous["commit"], artifacts={"linux-x64": artifact}, managers={"unix": manager}, generator={"origin": {"commit": previous["wharf"]}})
        self.seal_url = self.authority + "/" + release.sealed(self.placed)
        self.public[self.seal_url] = canonical.encode(self.seal)
        record = dict(self.identity, attempt={name: previous[name] for name in ("run", "attempt", "wharf")})
        self.public[self.authority + "/" + distribution.location(previous["marker"])] = canonical.encode(record)
        observed = {name: {"outputs": dict(decision="skip", presence="none")} for name in completion.MEDIA}
        observed["release"]["outputs"]["presence"] = "present"
        observed["channel"]["outputs"] = dict(decision="run", presence="present")
        self.held["steps"] = json.dumps(observed)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(release, "fetch", side_effect=lambda url: self.public[url]))
        self.stack.enter_context(mock.patch.object(completion.control, "trusted", return_value={"status": "completed"}))
        self.jobs = [{"name": "[plan] entries", "status": "completed", "conclusion": "success"}, {"name": "[build] binary linux", "status": "completed", "conclusion": "success"}]
        self.stack.enter_context(mock.patch.object(completion.publication, "jobs", side_effect=lambda identity: self.jobs))

    def object(self, name, raw):
        entry = release.remote(self.authority, "unused", raw, "application/octet-stream")
        entry.update(name=name, url=f"{self.authority}/v1/objects/sha256/{entry['sha256']}/{name}")
        self.public[entry["url"]] = raw
        return entry

    def prepare(self):
        return completion.prepare(self.held, self.bucket, self.expected)

    def test_existing_seal_records_only_observed_media_and_preserves_original_proof(self):
        before = dict(self.bucket.objects)
        context, entries = self.prepare()
        self.assertEqual(self.bucket.objects, before)
        self.assertEqual(context["snapshot"], self.expected)
        self.assertEqual(set(entries), set(completion.MEDIA))
        self.assertEqual([name for name, entry in entries.items() if entry["decision"] == "run"], ["channel"])
        self.assertEqual(context["completion"]["plan"], plan.reference(plan.read(self.bucket, self.previous)))

    def test_planner_never_enters_current_resolution_or_build_planning(self):
        with mock.patch.object(planner.r2, "configured", return_value=self.bucket), mock.patch.object(planner.basis, "carried", return_value=False), mock.patch.object(snapshot, "prepared", side_effect=AssertionError("new resolution")), mock.patch.object(planner.parameters, "answer"):
            result = planner.record(self.held)
        recorded = plan.read(self.bucket, self.held)
        self.assertEqual(result["entry"]["channel"], "run")
        self.assertEqual(recorded["shape"]["layers"], [])
        self.assertEqual(recorded["context"]["snapshot"], self.expected)

    def test_incomplete_or_changed_public_bytes_refuse_before_recording(self):
        for entry in [*self.seal["artifacts"].values(), *self.seal["managers"].values()]:
            with self.subTest(url=entry["url"]):
                body = self.public[entry["url"]]
                self.public[entry["url"]] = body + b"changed"
                self.assertRaisesRegex(Refusal, "object bytes", self.prepare)
                self.public[entry["url"]] = body
        self.assertFalse(self.bucket.exists(plan.location(self.held)))

    def test_missing_reservation_or_original_build_record_refuses(self):
        self.bucket.objects.pop(workload.prefix(self.key) + "record.json")
        self.assertRaisesRegex(Refusal, "record is missing", self.prepare)

    def test_active_or_failed_native_work_refuses(self):
        for field, value in (("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled")):
            with self.subTest(field=field, value=value):
                original = copy.deepcopy(self.jobs)
                self.jobs[1][field] = value
                self.assertRaises(Refusal, self.prepare)
                self.jobs[:] = original

    def test_guard_failure_is_not_reclassified_as_existing_publication(self):
        self.expected["guard"]["ok"] = False
        document = plan.document(self.previous, self.entries, {})
        self.bucket.objects[plan.location(self.previous)] = canonical.encode(document)
        self.assertRaisesRegex(Refusal, "successful full/head Guard", self.prepare)

    def test_mixed_media_or_windtun_are_outside_completion_scope(self):
        path = self.repository.root / "plumb.toml"
        original = path.read_text()
        for extra in ('[release.oci]\n', '[windtun]\n'):
            with self.subTest(extra=extra):
                path.write_text(original + extra)
                self.assertIsNone(self.prepare())
        path.write_text(original)

    def test_channel_revalidation_refuses_a_changed_seal(self):
        context, entries = self.prepare()
        self.public[self.seal_url] += b" "
        self.assertRaisesRegex(Refusal, "seal changed", completion.validate, self.bucket, dict(context, source=self.held["source"]), context["completion"])

    def test_retry_keeps_exact_original_publisher_through_completion_plans(self):
        context, entries = self.prepare()
        plan.record(self.bucket, context, entries, {})
        url = self.authority + "/" + distribution.location(self.held["marker"])
        self.public[url] = canonical.encode(dict(self.identity, attempt={name: context[name] for name in ("run", "attempt", "wharf")}))
        self.held["run"] = "9"
        repeated, _ = self.prepare()
        self.assertEqual(repeated["completion"], context["completion"])
