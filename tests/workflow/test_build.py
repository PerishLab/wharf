import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.content.static import evidence, source, workspace
from lib.media import node
from lib.process.lane import build, entry
from lib.refusal import Refusal
from lib.store import handoff
from lib.store.lane import state
from tests.lib.content.test_preview import target
from tests.lib.content.test_static import guarded
from tests.lib.media.test_preview import Builder, DOMAIN
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.store.test_handoff import receipt
from tests.workflow import fixtures


class RecordedBuild(unittest.TestCase):
    def prepared(self):
        builder = Builder()
        for directory in (builder.repository.root, builder.destination.parent, builder.bin):
            self.addCleanup(shutil.rmtree, directory)
        legacy = workspace.Source(builder.repository.root, builder.intent, target())
        seat = self.enterContext(workspace.prepare(legacy))
        builder.repository.root = seat.root
        builder.proof = guarded(builder.intent, seat.root)
        registered = fixtures.registered(generation=builder.intent["source"]["declaration"])
        intent = fixtures.request(target=registered)
        intent["source"] = copy.deepcopy(builder.intent["source"])
        api = fixtures.provider()
        api.source = {"sha": intent["source"]["commit"], "tree": {"sha": intent["source"]["tree"]}}
        observed = entry.admit(intent, fixtures.Store(registered), CONTEXT, api)
        held = {"request": fixtures.encoded(intent), "source": str(seat.root), "admitted": canonical.encode(observed)}
        env = builder.environment(dict(fixtures.platform(), WHARF_R2_ACCESS_KEY_ID="workload-only", WHARF_R2_SECRET_ACCESS_KEY="workload-only",
                                       WHARF_PACKAGES_TOKEN="package-reader"))
        return builder, held, env

    @staticmethod
    def run_build(builder, held, env, bucket):
        actual = node.preview_build

        def invoke(request, environ):
            return actual(request, builder.execute, builder.inspect, environ)

        with mock.patch.object(node, "preview_build", side_effect=invoke):
            with mock.patch.object(build.toolchain, "resolve", return_value=DOMAIN):
                with mock.patch.object(build.r2, "configured", return_value=bucket):
                    return build.record(held, env)

    def test_online_build_records_and_independently_fetches_exact_workload(self):
        builder, held, env = self.prepared()
        bucket = fixtures.Store(fixtures.registered())
        result = self.run_build(builder, held, env, bucket)
        document, files = handoff.fetch(bucket, result["workload"])
        self.assertEqual(result["artifact"]["digest"], document["content"])
        self.assertEqual(result["artifact"]["source"]["commit"], builder.intent["source"]["commit"])
        self.assertEqual(result["artifact"]["build"]["inputs"], document["key"])
        self.assertIn("index.html", files)
        for _, _, child in builder.calls:
            self.assertFalse(set(build.FORBIDDEN) & set(child))
            self.assertFalse({"WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY"} & set(child))
        self.assertEqual([call[1] for call in bucket.calls if call[0] == "create"][-1], "workload/1/" + result["workload"] + "/record.json")
        self.assertFalse(any(key.startswith(("lane/", "v1/releases/")) for key in bucket.objects))

    def test_writer_scope_and_moved_domain_refuse_before_product(self):
        builder, held, env = self.prepared()
        bucket = fixtures.Store(fixtures.registered())
        for key in build.FORBIDDEN:
            with self.subTest(key=key), self.assertRaises(Refusal):
                self.run_build(builder, held, dict(env, **{key: "forbidden"}), bucket)
        with mock.patch.object(build.toolchain, "resolve", return_value=dict(DOMAIN, **{"node.version": "25.0.0"})):
            with self.assertRaises(Refusal):
                build.record(held, env)
        self.assertFalse(builder.calls)
        self.assertFalse(bucket.objects)

    def test_guard_failure_and_unsafe_content_record_nothing(self):
        builder, held, env = self.prepared()
        builder.proof["ok"] = False
        bucket = fixtures.Store(fixtures.registered())
        with self.assertRaises(Refusal):
            self.run_build(builder, held, env, bucket)
        self.assertFalse(bucket.objects)
        self.assertFalse(any("build" in argv for _, argv, _ in builder.calls))

    def test_configuration_refusal_records_nothing_and_never_enters_guard(self):
        builder, held, env = self.prepared()
        original = builder.execute

        def execute(argv, cwd, child):
            original(argv, cwd, child)
            if "configuration" in argv:
                raise Refusal("trusted configuration failed")

        builder.execute = execute
        bucket = fixtures.Store(fixtures.registered())
        with self.assertRaisesRegex(Refusal, "configuration failed"):
            self.run_build(builder, held, env, bucket)
        self.assertFalse(any("guard" in argv or "build" in argv for _, argv, _ in builder.calls))
        self.assertFalse(bucket.objects)

    def test_configuration_source_or_tool_movement_refuses_before_guard(self):
        for changed in ("source", "tool"):
            builder, held, env = self.prepared()
            original = builder.execute

            def execute(argv, cwd, child):
                original(argv, cwd, child)
                if "configuration" in argv:
                    path = builder.repository.root / "plumb.toml" if changed == "source" else builder.bin / "plumb"
                    path.write_text("changed")

            builder.execute = execute
            bucket = fixtures.Store(fixtures.registered())
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                self.run_build(builder, held, env, bucket)
            self.assertFalse(any("guard" in argv or "build" in argv for _, argv, _ in builder.calls))
            self.assertFalse(bucket.objects)

    def test_admission_from_other_run_or_altered_registration_refuses(self):
        builder, held, env = self.prepared()
        for path in (("workflow", "run"), ("workflow", "attempt"), ("registration", "enabled")):
            changed = json.loads(held["admitted"])
            changed[path[0]][path[1]] = False if path[1] == "enabled" else 999
            with self.subTest(path=path), self.assertRaises(Refusal):
                self.run_build(builder, dict(held, admitted=json.dumps(changed)), env, fixtures.Store(fixtures.registered()))
        self.assertFalse(builder.calls)

    def test_real_product_process_receives_no_outer_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            env = evidence.clean(dict(os.environ, WHARF_R2_SECRET_ACCESS_KEY="writer", WHARF_LANE_PROVIDER_TOKEN="writer"), directory)
            node.preview_execute(["/bin/sh", "-c", 'test -z "$WHARF_R2_SECRET_ACCESS_KEY" && test -z "$WHARF_LANE_PROVIDER_TOKEN"'], directory, env)

    def test_source_qualification_requires_shared_full_generation(self):
        builder, held, env = self.prepared()
        requested = entry.requested(held["request"])
        observed = json.loads(held["admitted"])
        selected = entry.source(requested, observed)
        configuration = source.qualify(builder.repository.root, selected, observed["registration"], evidence.clean(env, tempfile.gettempdir()))
        self.assertEqual(configuration["digest"], observed["registration"]["generation"])
        changed = dict(selected, source=dict(selected["source"], declaration="0" * 64))
        with self.assertRaises(Refusal):
            source.qualify(builder.repository.root, changed, observed["registration"], evidence.clean(env, tempfile.gettempdir()))
        self.assertEqual(state.initial(observed["registration"])["phase"], "vacant")

    def test_workflow_build_has_one_bucket_writer_and_no_reader_of_lane_state(self):
        root = Path(build.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/lane-intake.yml").read_text()
        product = workflow.split("\n  build:\n", 1)[1].split("\n  reserve:\n", 1)[0]
        self.assertIn("WHARF_R2_SECRET_ACCESS_KEY:", product)
        self.assertNotIn("WHARF_LANE_READER_", product)
        self.assertNotIn("CLOUDFLARE", product)
        self.assertNotIn("LANE_STATE_SECRET", product)
        self.assertNotIn("LANE_EVIDENCE_SECRET", product)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn("needs: [admission]", product)
        self.assertIn("needs.admission.outputs.operation == 'apply'", product)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("group: wharf-static-lane-intake", workflow)

    def test_handoff_world_pins_the_shared_lane_contract_resource(self):
        held = receipt()
        self.assertIn("resource:identity/lane.json", held["implementation"])
        self.assertEqual(handoff.recipe(held), held)
        original = handoff.implementation.resources.read_bytes

        def changed(name):
            return original(name) + (b" " if name == "identity/lane.json" else b"")

        with mock.patch.object(handoff.implementation.resources, "read_bytes", side_effect=changed):
            with self.assertRaises(Refusal):
                handoff.recipe(held)


if __name__ == "__main__":
    unittest.main()
