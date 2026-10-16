import copy
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path, PurePosixPath
from unittest import mock

from lib.content import canonical
from lib.content.static import assets, evidence
from lib.process.cfworker import apply, create, payload, reader, upload, writer
from lib.process.lane import deployment, entry, execution, provider, reservation, transfer
from lib.refusal import Refusal
from lib.store import handoff
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.process import test_cfworker_upload
from tests.lib.process.test_cfworker import Response
from tests.domains.test_routing import qualification
from tests.workflow import fixtures, test_transfer
from tests.workflow.provider import test_provider
from tests.workflow.provider.fixtures import configure


class Deployment(unittest.TestCase):
    def prepared(self, selected=False):
        fixture = test_transfer.Transfer()
        self.addCleanup(fixture.doCleanups)
        intent, store, bucket, held = fixture.bundle()
        env = dict(fixtures.platform(), WHARF_LANE_PARENT_ID="parent-id", WHARF_LANE_SUBDOMAIN="development",
                   WHARF_LANE_PROVIDER_READER_TOKEN="reader", GITHUB_JOB="apply", RUNNER_TEMP=self.enterContext(tempfile.TemporaryDirectory()))
        parent = {"id": "parent-id", "name": "crest-review", "account": "a" * 32,
                  "suffix": "-crest-review.development.workers.dev"}
        preview = {"id": "selected-id", "name": "preview", "parent": "parent-id"}
        configure(env, parent, "crest-review")
        current = {"parent": parent, "previews": [preview] if selected else [], "preview": preview if selected else None, "deployments": []}
        api = mock.Mock()
        api.status.side_effect = lambda policy: copy.deepcopy({key: current[key] for key in ("parent", "preview", "previews")})
        api.history.side_effect = lambda policy: copy.deepcopy(current)
        api.observe.side_effect = lambda policy, identity: qualification(current, next(item for item in current["deployments"] if item["id"] == identity))
        source = test_provider.Provider.invoke(store, bucket, held, (env, api))
        held["provider"] = fixtures.encoded(source)
        env["WHARF_LANE_PROVIDER_TOKEN"] = "private-provider-writer"
        return {"store": store, "bucket": bucket, "held": held, "env": env, "api": api, "current": current, "preview": preview}

    @staticmethod
    def invoke(fixture, failure=None):
        store, bucket, held, env = (fixture[key] for key in ("store", "bucket", "held", "env"))
        artifact = evidence.decode(held["prepared"])["request"]["intent"]["artifact"]
        document, files = handoff.fetch(bucket, artifact["build"][apply.BUILD[0]])
        transport, reads = mock.Mock(), mock.Mock()
        deployed = {"id": "12345678-1234-1234-1234-123456789abc", "preview": "selected-id", "parent": "parent-id"}
        if failure == "routing":
            fixture["api"].observe.side_effect = Refusal("unqualified routing")
        if failure == "routing-moved":
            fixture["api"].observe.side_effect = lambda policy, identity: qualification(fixture["current"], deployed) if fixture["api"].observe.call_count == 1 else {}
        groups, done = [], []

        def observe(intent, value, environ):
            return execution.admit(intent, value, store, (entry.platform(environ), fixtures.provider()))

        def received(request, timeout):
            url = request.full_url
            status = 200
            if url.endswith("/previews?ignore_base_config=true"):
                stage = "create"
                fixture["current"].update(preview=fixture["preview"], previews=[fixture["preview"]])
                result = dict(fixture["preview"], slug="preview")
            elif url.endswith("/assets-upload-session"):
                stage = "session"
                manifest = json.loads(request.data)["manifest"]
                hashes = list(dict.fromkeys(value["hash"] for value in manifest.values()))
                groups.extend([[hashes[0]], hashes[1:]])
                result = {"jwt": test_cfworker_upload.JWT, "buckets": groups}
            elif url.endswith("/workers/assets/upload?base64=true"):
                stage = "bucket"
                done.append(url)
                status = 201 if len(done) == len(groups) else 200
                result = {"jwt": test_cfworker_upload.FINAL} if status == 201 else {}
            else:
                stage = "deploy"
                fixture["current"]["deployments"].append(deployed)
                result = {"id": deployed["id"], "preview_id": "selected-id", "preview_name": "preview"}
            if stage == failure:
                raise TimeoutError("ambiguous acknowledgement")
            response = Response(canonical.encode({"success": True, "errors": [], "result": result}), url)
            response.status = status
            return response

        def content(request, timeout):
            immutable = "https://" + deployed["id"][:8] + "-preview.lane.crest-review.perish.uk"
            current = "https://preview.lane.crest-review.perish.uk"
            origin = immutable if request.full_url.startswith(immutable) else current
            name, body = payload.routes(files)[request.full_url.removeprefix(origin)]
            response = Response(b"changed" if failure == "bytes" or failure == "current" and origin == current else body, request.full_url)
            response.headers = Message()
            for key, value in {"Content-Security-Policy": assets.CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                               "X-Robots-Tag": "noindex, nofollow", "Content-Type": payload.MIME[PurePosixPath(name).suffix.lower()], "Content-Length": str(len(body))}.items():
                response.headers[key] = value
            if failure == "indexing":
                response.headers.replace_header("X-Robots-Tag", "noindex")
            if failure == "authority":
                store.target["enabled"] = False
            return response

        def owned(actual):
            def configured(*values):
                result = actual(*values)
                result._client = transport
                return result
            return configured

        actual = reader.Readback
        def readback(plan, observed, identity):
            result = actual(plan, observed, identity)
            result._client = reads
            return result

        transport.open.side_effect, reads.open.side_effect = received, content
        with mock.patch.object(execution, "observe", side_effect=observe), mock.patch.object(transfer, "Reader", return_value=bucket):
            with mock.patch.object(reader, "Reader", return_value=fixture["api"]), mock.patch.object(reader, "Readback", side_effect=readback):
                with mock.patch.object(create, "Creator", side_effect=owned(create.Creator)), mock.patch.object(upload, "Uploader", side_effect=owned(upload.Uploader)):
                    with mock.patch.object(writer, "Deployer", side_effect=owned(writer.Deployer)):
                        fixture.update(transport=transport, reads=reads)
                        return deployment.perform(held, env)

    def test_created_or_existing_preview_uploads_once_and_reads_both_routes(self):
        for selected in (False, True):
            fixture = self.prepared(selected)
            before = copy.deepcopy(fixture["store"].current)
            result = self.invoke(fixture)
            calls = fixture["transport"].open.call_args_list
            self.assertEqual(len(calls), 4 if selected else 5)
            self.assertTrue(all(call.args[0].get_method() == "POST" for call in calls))
            self.assertTrue(calls[-1].args[0].full_url.endswith("/previews/selected-id/deployments?ignore_defaults=true"))
            self.assertEqual(result["operation"]["outcome"]["material"]["artifact"], result["plan"]["apply"]["artifact"])
            self.assertEqual(result["operation"]["outcome"]["writer"], "idle")
            self.assertEqual(fixture["store"].current, before)
            urls = [call.args[0].full_url for call in fixture["reads"].open.call_args_list]
            self.assertTrue(any(url.startswith("https://preview.lane.crest-review.perish.uk/") for url in urls))
            self.assertTrue(any(url.startswith("https://12345678-") for url in urls))
            self.assertTrue(all(".lane.crest-review.perish.uk/" in url and ".workers.dev" not in url for url in urls))
            self.assertIn("domain", result["plan"]["policy"])
            for private in (test_cfworker_upload.JWT, test_cfworker_upload.FINAL, "private-provider-writer"):
                self.assertNotIn(private, fixtures.encoded(result))
            self.assertEqual(len(list(Path(fixture["env"]["RUNNER_TEMP"]).iterdir())), 1)

    def test_ambiguous_each_write_stops_without_retry_or_deploy_continuation(self):
        for failure, count in (("create", 1), ("session", 2), ("bucket", 3), ("deploy", 5)):
            fixture = self.prepared()
            result = self.invoke(fixture, failure)
            self.assertEqual(result["operation"]["outcome"], {"outcome": "unknown", "reason": "unverified"})
            self.assertEqual(fixture["transport"].open.call_count, count)
            fixture["reads"].open.assert_not_called()
            self.assertEqual(fixture["store"].current["phase"], "reserved")
            with self.assertRaises(Refusal):
                self.invoke(fixture)
            fixture["transport"].open.assert_not_called()

    def test_failed_immutable_or_current_content_or_authority_never_applies(self):
        for failure in ("bytes", "current", "authority", "routing", "routing-moved", "indexing"):
            fixture = self.prepared()
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.invoke(fixture, failure)
            self.assertEqual(fixture["transport"].open.call_count, 5)
            self.assertEqual(fixture["store"].current["phase"], "reserved")
            if failure == "routing":
                fixture["reads"].open.assert_not_called()

    def test_foreign_attempt_job_selector_preflight_or_writer_never_writes(self):
        for failure in ("attempt", "job", "workload", "preflight", "writer"):
            fixture = self.prepared()
            if failure == "attempt":
                fixture["env"]["GITHUB_RUN_ATTEMPT"] = "2"
            elif failure == "job":
                fixture["env"]["GITHUB_JOB"] = "build"
            elif failure == "workload":
                fixture["held"]["workload"] = "0" * 64
            elif failure == "preflight":
                projected = evidence.decode(fixture["held"]["provider"])
                projected["execution"] = "0" * 64
                fixture["held"]["provider"] = fixtures.encoded(projected)
            else:
                fixture["env"]["WHARF_R2_SECRET_ACCESS_KEY"] = "writer"
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.invoke(fixture)
            fixture["transport"].open.assert_not_called()

    def test_provider_or_authority_movement_before_deployment_blocks_last_write(self):
        for failure, stage in [(failure, stage) for failure in ("provider", "authority", "cancelled") for stage in range(2, 6)]:
            fixture = self.prepared(True)
            original = fixture["api"].history.side_effect
            calls = []
            control = fixtures.provider()

            def moved(policy):
                calls.append(policy)
                result = original(policy)
                if len(calls) == stage:
                    if failure == "provider":
                        result["previews"] = []
                    elif failure == "cancelled":
                        control.observed.update(status="completed", conclusion="cancelled")
                    else:
                        fixture["store"].target["enabled"] = False
                return result

            fixture["api"].history.side_effect = moved
            with mock.patch.object(fixtures, "provider", return_value=control), self.subTest(failure=failure, stage=stage), self.assertRaises(Refusal):
                self.invoke(fixture)
            self.assertEqual(fixture["transport"].open.call_count, stage - 2)
            fixture["reads"].open.assert_not_called()

    def test_reusable_job_explicitly_receives_only_provider_writer_and_readers(self):
        root = Path(deployment.__file__).resolve().parents[3]
        invoked = (root / ".github/workflows/lane-deploy.yml").read_text()
        self.assertIn("workflow_call:", invoked)
        self.assertIn("value: ${{ jobs.apply.outputs.result }}", invoked)
        self.assertIn("WHARF_LANE_DEPLOYMENT_TOKEN:", invoked)
        self.assertNotIn("secrets: inherit", invoked)
        self.assertIn("ref: ${{ github.sha }}", invoked)
        self.assertNotIn("workflow_dispatch", invoked)
        for text in (invoked, (root / ".github/actions/lane-deployment/action.yml").read_text()):
            for name in ("WHARF_LANE_STATE_SECRET", "WHARF_LANE_EVIDENCE_SECRET", "preview-source", "WHARF_CLOUDFLARE_TOKEN"):
                self.assertNotIn(name, text)

    def test_saved_handoff_changed_after_consumer_never_reaches_writer(self):
        fixture = self.prepared()
        original = provider.content

        def changed(artifact, environ):
            document, bodies = original(artifact, environ)
            return document, lambda digest: b"changed"

        with mock.patch.object(provider, "content", side_effect=changed), self.assertRaises(Refusal):
            self.invoke(fixture)
        fixture["transport"].open.assert_not_called()

    def test_present_material_retains_prior_deployments_and_current_proof_cannot_settle(self):
        fixture = self.prepared(True)
        registered = fixture["store"].target
        intent = evidence.decode(fixture["held"]["request"])
        store = fixtures.Store(registered, fixtures.settled(registered))
        intent.update(request="e" * 64, revision=store.current["revision"], expected=store.current["observed"])
        artifact = evidence.decode(fixture["held"]["prepared"])["request"]["intent"]["artifact"]
        admitted = entry.admit(intent, store, CONTEXT, fixtures.provider())
        with mock.patch.object(entry, "observe", return_value=admitted), mock.patch.object(reservation.r2, "configured", return_value=store):
            reserved = reservation.prepare({"request": fixtures.encoded(intent), "artifact": fixtures.encoded(artifact)}, fixture["env"])
        fixture["store"] = store
        fixture["held"].update(request=fixtures.encoded(intent), prepared=fixtures.encoded(reserved))
        previous = {"id": "abcdefab-1234-1234-1234-000000000000", "preview": "selected-id", "parent": "parent-id"}
        fixture["current"]["deployments"] = [previous]
        env = {name: value for name, value in fixture["env"].items() if name != "WHARF_LANE_PROVIDER_TOKEN"}
        preflight = test_provider.Provider.invoke(store, fixture["bucket"], fixture["held"], (env, fixture["api"]))
        fixture["held"]["provider"] = fixtures.encoded(preflight)
        result = self.invoke(fixture)
        self.assertIn(previous, fixture["current"]["deployments"])
        self.assertEqual(len(fixture["current"]["deployments"]), 2)
        document, files = handoff.fetch(fixture["bucket"], fixture["held"]["workload"])
        bodies = {item["blob"]: files[name] for name, item in document["files"].items()}
        client = reader.Readback(result["plan"], fixture["current"], qualification(fixture["current"], fixture["current"]["deployments"][-1])).current().current()
        with mock.patch.object(client, "_get"):
            proof = client.read(document, bodies.__getitem__)
        with self.assertRaises(Refusal):
            apply.settle(result["plan"], fixture["current"], proof, "idle")


if __name__ == "__main__":
    unittest.main()
