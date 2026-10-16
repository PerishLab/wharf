import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.process.cfworker import apply, reader
from lib.process.lane import entry, execution, provider, reservation, transfer
from lib.refusal import Refusal
from lib.store import handoff
from tests.cfworker import test_inspection
from tests.lib.content.test_assets import FILES
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.store.memory import Memory
from tests.lib.process.test_cfworker import Response
from tests.workflow import fixtures, test_transfer
from tests.workflow.provider.fixtures import configure
from tests.workflow.test_execution import prepared


class Provider(unittest.TestCase):
    def present(self):
        fixture = test_inspection.Inspection()
        fixture.setUp()
        registered = fixture.registered
        intent = fixtures.request("inspect", registered)
        intent.update(revision=fixture.current["revision"], expected=fixture.current["observed"])
        store = fixtures.Store(registered, fixture.current)
        observed = entry.admit(intent, store, CONTEXT, fixtures.provider())
        held = {"request": fixtures.encoded(intent), "artifact": "null"}
        with mock.patch.object(entry, "observe", return_value=observed):
            value = reservation.prepare(held, fixtures.platform())
        directory = self.enterContext(tempfile.TemporaryDirectory())
        destination = Path(directory) / "content"
        handoff.pack(destination, FILES, fixture.fixture.fixture.fixture.receipt)
        bucket = Memory()
        handoff.publish(bucket, destination)
        env = dict(fixtures.platform(), WHARF_LANE_PARENT_ID=fixture.plan["policy"]["id"],
                   WHARF_LANE_SUBDOMAIN=fixture.plan["policy"]["subdomain"], WHARF_LANE_PROVIDER_READER_TOKEN="reader")
        held = {"request": held["request"], "prepared": fixtures.encoded(value), "workload": "none"}
        fixture.plan["policy"] = configure(env, fixture.observed["parent"])
        fixture.origin = "https://preview.lane.crest.perish.uk"
        return fixture, intent, store, bucket, env, held

    @staticmethod
    def invoke(store, bucket, held, trusted):
        env, api = trusted
        def observed(request, value, environ):
            return execution.admit(request, value, store, (entry.platform(environ), fixtures.provider()))

        with mock.patch.object(execution, "observe", side_effect=observed):
            with mock.patch.object(transfer, "Reader", return_value=bucket), mock.patch.object(provider.reader, "Reader", return_value=api):
                return provider.observe(held, env)

    def test_present_inspection_reads_saved_workload_and_every_current_asset(self):
        fixture, intent, store, bucket, env, held = self.present()
        api = mock.Mock()
        api.status.return_value = fixture.observed
        actual = reader.Inspection
        clients = []
        transport = fixture.client()._client

        def client(plan, observed):
            result = actual(plan, observed)
            result._client = transport
            clients.append(result)
            return result

        before = copy.deepcopy(store.current)
        with mock.patch.object(reader, "Inspection", side_effect=client):
            result = self.invoke(store, bucket, held, (env, api))
        self.assertEqual(result["operation"]["outcome"]["material"], fixture.operation["outcome"]["material"])
        self.assertEqual(result["operation"]["request"]["context"]["revision"], before["revision"])
        self.assertEqual(len(clients[0]._client.open.call_args_list), len(fixture.fixture.fixture.fixture.files) - 1)
        self.assertEqual(store.current, before)
        self.assertEqual(api.status.call_count, 2)
        self.assertTrue(all(call.args[0]["domain"] == fixture.plan["policy"]["domain"] for call in api.status.call_args_list))
        self.assertTrue(all(call.args[0].full_url.startswith("https://preview.lane.crest.perish.uk/") for call in clients[0]._client.open.call_args_list))

    def test_absent_inspection_consumes_no_content_or_asset_http(self):
        fixture, intent, store, bucket, env, held = self.present()
        store.current = None
        intent.update(revision=1, expected={"state": "absent"})
        observed = entry.admit(intent, store, CONTEXT, fixtures.provider())
        with mock.patch.object(entry, "observe", return_value=observed):
            value = reservation.prepare({"request": fixtures.encoded(intent), "artifact": "null"}, env)
        held.update(request=fixtures.encoded(intent), prepared=fixtures.encoded(value))
        api = mock.Mock()
        api.status.return_value = dict(fixture.observed, preview=None, previews=[fixture.observed["previews"][1]])
        with mock.patch.object(provider, "content", side_effect=AssertionError("absent consumes no saved content")):
            result = self.invoke(store, None, held, (env, api))
        self.assertEqual(result["operation"]["outcome"], {"outcome": "applied", "material": {"state": "absent"}, "writer": "idle"})
        self.assertIsNone(store.current)

    def test_provider_failure_is_not_absence_or_retry(self):
        fixture, intent, store, bucket, env, held = self.present()
        api = mock.Mock()
        api.status.side_effect = Refusal("provider unavailable")
        with self.assertRaises(Refusal):
            self.invoke(store, bucket, held, (env, api))
        self.assertEqual(api.status.call_count, 1)
        with mock.patch.object(provider, "content", side_effect=Refusal("saved content unavailable")):
            api.status.side_effect = None
            api.status.return_value = fixture.observed
            with self.assertRaises(Refusal):
                self.invoke(store, bucket, held, (env, api))

    def test_wrong_routing_bytes_or_headers_never_return_an_operation(self):
        for failure in ("presence", "bytes", "headers"):
            fixture, intent, store, bucket, env, held = self.present()
            api = mock.Mock()
            api.status.return_value = fixture.observed
            if failure == "presence":
                api.status.return_value = dict(fixture.observed, preview=None, previews=[])
                with self.assertRaises(Refusal):
                    self.invoke(store, bucket, held, (env, api))
                continue
            actual = reader.Inspection
            transport = fixture.client()._client

            def client(plan, observed):
                result = actual(plan, observed)
                result._client = transport
                received = result._client.open.side_effect

                def changed(request, timeout):
                    response = received(request, timeout)
                    if failure == "bytes":
                        changed = Response(b"changed", request.full_url)
                        changed.headers = response.headers
                        response = changed
                    else:
                        response.headers.replace_header("Content-Security-Policy", "default-src *")
                    return response

                result._client.open.side_effect = changed
                return result

            with mock.patch.object(reader, "Inspection", side_effect=client), self.assertRaises(Refusal):
                self.invoke(store, bucket, held, (env, api))

    def test_provider_or_registration_movement_after_reads_blocks_result(self):
        for failure in ("provider", "registration"):
            fixture, intent, store, bucket, env, held = self.present()
            api = mock.Mock()
            count = 0

            def status(policy):
                nonlocal count
                count += 1
                if count == 2:
                    if failure == "provider":
                        return dict(fixture.observed, previews=[])
                    store.target["enabled"] = False
                return fixture.observed

            api.status.side_effect = status
            with mock.patch.object(provider, "inspection", return_value=(fixture.plan, fixture.operation)), self.assertRaises(Refusal):
                self.invoke(store, bucket, held, (env, api))

    def test_disposal_only_produces_a_readonly_plan(self):
        fixture, _, _, _, env, _ = self.present()
        intent, store, value = prepared("dispose")
        registered = copy.deepcopy(value["registration"])
        registered["mapping"]["resource"] = "crest-development"
        registered["declaration"]["target"]["app"] = "crest"
        intent = fixtures.request("dispose", registered)
        store = fixtures.Store(registered, fixtures.settled(registered))
        observed = entry.admit(intent, store, CONTEXT, fixtures.provider())
        with mock.patch.object(entry, "observe", return_value=observed), mock.patch.object(reservation.r2, "configured", return_value=store):
            value = reservation.prepare({"request": fixtures.encoded(intent), "artifact": "null"}, env)
        api = mock.Mock()
        api.status.return_value = fixture.observed
        held = {"request": fixtures.encoded(intent), "prepared": fixtures.encoded(value), "workload": "none"}
        result = self.invoke(store, None, held, (env, api))
        self.assertIsNone(result["operation"])
        self.assertEqual(result["plan"]["current"]["active"], store.current["active"])
        self.assertEqual(store.current["phase"], "reserved")

    def test_reusable_observer_contains_only_readers_and_no_partial_trigger(self):
        root = Path(provider.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/lane-observe.yml").read_text()
        self.assertIn("workflow_call:", workflow)
        self.assertIn("lane-consume", workflow)
        self.assertIn("lane-observe", workflow)
        self.assertIn("value: ${{ jobs.observe.outputs.provider }}", workflow)
        for forbidden in ("workflow_dispatch:", "push:", "schedule:", "secrets: inherit", "preview-source", "lane-build",
                          "WHARF_R2_ACCESS_KEY_ID:", "WHARF_R2_SECRET_ACCESS_KEY:", "WHARF_LANE_STATE_ACCESS_KEY_ID:",
                          "WHARF_LANE_EVIDENCE_", "WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "upload-artifact"):
            self.assertNotIn(forbidden, workflow)

    def test_missing_parent_pins_or_writer_environment_refuse_before_provider(self):
        for env in ({}, {"WHARF_LANE_PARENT_ID": "foreign"}, {"WHARF_LANE_SUBDOMAIN": "unknown"}):
            with self.assertRaises(Refusal):
                provider.policy(env, {})
        with mock.patch.object(execution, "observe", side_effect=AssertionError("writer refused before reads")), self.assertRaises(Refusal):
            provider.observe({}, {"WHARF_LANE_PROVIDER_TOKEN": "writer"})

    def test_apply_binds_real_recorded_content_to_creation_or_deployment_preflight(self):
        fixture = test_transfer.Transfer()
        self.addCleanup(fixture.doCleanups)
        intent, store, bucket, held = fixture.bundle()
        env = dict(fixtures.platform(), WHARF_LANE_PARENT_ID="parent-id", WHARF_LANE_SUBDOMAIN="development",
                   WHARF_LANE_PROVIDER_READER_TOKEN="reader")
        parent = {"id": "parent-id", "name": "crest-review", "account": "a" * 32,
                  "suffix": "-crest-review.development.workers.dev"}
        status = {"parent": parent, "previews": [], "preview": None}
        configure(env, parent, "crest-review")
        api = mock.Mock()
        api.status.return_value = status
        created = self.invoke(store, bucket, held, (env, api))
        self.assertIsNone(created["operation"])
        self.assertEqual(created["plan"]["apply"]["artifact"], store.current["active"]["request"]["intent"]["artifact"])
        selected = {"id": "selected-id", "name": "preview", "parent": "parent-id"}
        api.status.return_value = dict(status, previews=[selected], preview=selected)
        api.history.return_value = dict(api.status.return_value, deployments=[])
        deployed = self.invoke(store, bucket, held, (env, api))
        self.assertIsNone(deployed["operation"])
        self.assertEqual(deployed["plan"]["before"]["deployments"], [])
        self.assertEqual(deployed["plan"]["apply"]["workload"], held["workload"])
        api.history.assert_called_once()
        api.history.return_value = dict(api.status.return_value, deployments=[{"id": "invalid"}])
        with self.assertRaises(Refusal):
            self.invoke(store, bucket, held, (env, api))
        self.assertEqual(store.current["phase"], "reserved")


if __name__ == "__main__":
    unittest.main()
