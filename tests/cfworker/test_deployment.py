import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import apply, observation, payload, writer
from lib.refusal import Refusal
from lib.store.lane import state
from tests.lib.process import test_cfworker_apply, test_cfworker_observation, test_cfworker_upload
from tests.lib.process.test_cfworker import Response
from tests.lib.store.test_lane import operation


class Deployment(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_apply.Apply()
        fixture.setUp()
        self.fixture = fixture
        self.document, self.bodies = fixture.document, fixture.bodies
        provider = test_cfworker_observation.Observation()
        provider.setUp()
        self.policy = provider.policy
        self.parent = observation.parent(provider.parent, provider.selected, self.policy)
        self.preview = {"id": "selected-id", "name": "preview", "parent": self.parent["id"]}
        self.sibling = {"id": "sibling-id", "name": "sibling", "parent": self.parent["id"]}
        self.before = {"parent": self.parent, "previews": [self.preview, self.sibling], "preview": self.preview, "deployments": []}
        self.plan = apply.deployment_prepare(fixture.plan, self.before, self.policy)

    def raw(self, number=1):
        return {"id": f"12345678-1234-1234-1234-{number:012x}", "preview_id": self.preview["id"], "preview_name": self.preview["name"]}

    def projected(self, number=1):
        value = self.raw(number)
        return observation.deployment(value, self.preview, value["id"])

    def populated(self):
        plan = self.fixture.plan
        active = plan["current"]["active"]
        completed = dict(active, outcome={"outcome": "applied", "material": {"state": "present", "artifact": plan["artifact"]}, "writer": "idle"})
        previous = state.finished(plan["current"], completed, plan["registration"])
        value = operation(revision=2, identity="7", expected=previous["observed"])
        value["request"]["intent"]["artifact"] = plan["artifact"]
        value["assessment"]["request"] = codec.digest("request", value["request"])
        current = state.started(previous, {key: value[key] for key in ("request", "assessment")}, plan["registration"])
        material = apply.prepare(plan["registration"], current, self.document, self.bodies.__getitem__)
        self.before["deployments"] = [self.projected(0)]
        return apply.deployment_prepare(material, self.before, self.policy)

    def client(self, plan=None):
        api = writer.Deployer("private-deploy-credential", self.plan if plan is None else plan, test_cfworker_upload.FINAL)
        api._client = mock.Mock()
        self.response(api, self.raw())
        return api

    def response(self, api, value, status=200):
        body = json.dumps({"success": True, "errors": [], "result": value}).encode()
        def received(request, timeout):
            response = Response(body, request.full_url)
            response.status = status
            return response
        api._client.open.side_effect = received

    def invoke(self, api, observed=None):
        return api.deploy(self.before if observed is None else observed, self.document, self.bodies.__getitem__)

    def test_default_and_two_keyed_writes_bind_exact_immutable_preview_and_payload(self):
        for lane in ("preview", "preview.design", "preview.review"):
            self.preview["name"] = lane.replace(".", "-")
            plan = apply.deployment_prepare(self.fixture.prepared(lane), self.before, self.policy)
            api = self.client(plan)
            result = self.invoke(api)
            self.assertTrue(result["acknowledged"])
            request = api._client.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/workers/crest-development/previews/selected-id/deployments?ignore_defaults=true")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.data, payload.deployment(plan["apply"], self.document, self.bodies.__getitem__, test_cfworker_upload.FINAL)[1])
            self.assertEqual(request.get_header("Authorization"), "Bearer private-deploy-credential")
            self.assertEqual(request.get_header("Content-length"), str(len(request.data)))
            self.assertEqual(api._client.open.call_args.kwargs, {"timeout": 30})
            after = dict(self.before, deployments=[result["deployment"]])
            self.assertEqual(apply.deployment_confirm(plan, after, result["deployment"]), result["deployment"])

    def test_existing_deployments_are_retained_and_order_is_not_movement(self):
        self.plan = self.populated()
        api = self.client()
        result = self.invoke(api)
        after = dict(self.before, deployments=[result["deployment"], *self.before["deployments"]])
        self.assertEqual(apply.deployment_confirm(self.plan, after, result["deployment"]), result["deployment"])
        self.assertEqual(len(after["deployments"]), 2)

    def test_safe_acknowledgement_excludes_private_fields_and_is_not_applied_content(self):
        api = self.client()
        self.response(api, dict(self.raw(), env={"SECRET": "private-provider-value"}, urls=["https://foreign.invalid"], annotations={"digest": "not-proof"}), 201)
        result = self.invoke(api)
        self.assertEqual(set(result), {"request", "acknowledged", "deployment"})
        self.assertEqual(set(result["deployment"]), {"id", "preview", "parent"})
        text = json.dumps([self.plan, result])
        for private in ("private-deploy-credential", test_cfworker_upload.FINAL, "private-provider-value", "foreign.invalid", "not-proof"):
            self.assertNotIn(private, text)
        self.assertNotIn("outcome", result)

    def test_native_quota_headroom_refuses_before_writer_custody(self):
        self.plan = self.populated()
        for count in (98, 99, 100):
            plan = copy.deepcopy(self.plan)
            plan["before"]["deployments"] = [self.projected(number) for number in range(count)]
            if count == 98:
                self.client(plan)
            else:
                with self.assertRaises(Refusal):
                    self.client(plan)

    def test_reserved_absence_and_presence_must_match_retained_material(self):
        plan = copy.deepcopy(self.plan)
        plan["before"]["deployments"] = [self.projected(0)]
        with self.assertRaises(Refusal):
            self.client(plan)
        plan = self.populated()
        plan["before"]["deployments"] = []
        with self.assertRaises(Refusal):
            self.client(plan)

    def test_pre_write_parent_preview_sibling_or_deployment_drift_refuses_without_attempt(self):
        for field in ("parent", "preview", "sibling", "deployments"):
            api = self.client()
            observed = copy.deepcopy(self.before)
            if field == "parent":
                observed["parent"]["id"] = "foreign"
            elif field == "preview":
                observed["preview"]["id"] = "recreated"
            elif field == "sibling":
                observed["previews"][1]["id"] = "recreated"
            else:
                observed["deployments"] = [self.projected(0)]
            with self.assertRaises(Refusal):
                self.invoke(api, observed)
            api._client.open.assert_not_called()
            self.assertTrue(self.invoke(api)["acknowledged"])

    def test_disabled_stale_unknown_and_private_or_malformed_plans_refuse(self):
        for field, value in (("phase", "unknown"), ("revision", 99), ("registration", "0" * 64)):
            plan = copy.deepcopy(self.plan)
            plan["apply"]["current"][field] = value
            with self.assertRaises(Refusal):
                self.client(plan)
        plan = copy.deepcopy(self.plan)
        plan["apply"]["registration"]["enabled"] = False
        for value in (plan, {}, dict(self.plan, token="never"), dict(self.plan, before="x" * 65537)):
            with self.assertRaises(Refusal):
                self.client(value)

    def test_ambiguous_mutable_name_routing_and_absent_selection_refuse(self):
        for identity in ("preview", "sibling"):
            plan = copy.deepcopy(self.plan)
            plan["before"]["preview"]["id"] = identity
            plan["before"]["previews"][0]["id"] = identity
            with self.assertRaisesRegex(Refusal, "unambiguous"):
                self.client(plan)
        before = dict(self.before, preview=None, previews=[self.sibling])
        with self.assertRaises(Refusal):
            apply.deployment_prepare(self.fixture.plan, before, self.policy)

    def test_each_write_revalidates_exact_handoff_before_consuming_attempt(self):
        api = self.client()
        key = self.document["files"]["index.html"]["blob"]
        held = self.bodies[key]
        self.bodies[key] = b"changed"
        with self.assertRaises(Refusal):
            self.invoke(api)
        api._client.open.assert_not_called()
        self.bodies[key] = held
        self.assertTrue(self.invoke(api)["acknowledged"])

    def test_success_and_response_loss_consume_the_only_attempt(self):
        for error in (None, TimeoutError("private-deploy-credential"), OSError(test_cfworker_upload.FINAL)):
            api = self.client()
            if error is not None:
                api._client.open.side_effect = error
            result = self.invoke(api)
            self.assertEqual(result["acknowledged"], error is None)
            if error is not None:
                self.assertIsNone(result["deployment"])
            with self.assertRaises(Refusal):
                self.invoke(api)
            self.assertEqual(api._client.open.call_count, 1)

    def test_malformed_foreign_repeated_and_latest_reply_identities_are_unknown(self):
        self.plan = self.populated()
        for change in ({"id": "latest"}, {"id": self.raw(0)["id"]}, {"preview_id": "foreign"}, {"preview_name": "foreign"}, {"id": None}):
            api = self.client()
            self.response(api, dict(self.raw(), **change))
            self.assertFalse(self.invoke(api)["acknowledged"])
            with self.assertRaises(Refusal):
                self.invoke(api)

    def test_invalid_redirected_transformed_or_cleanup_replies_are_unknown(self):
        for body in (b'{"success":true,"success":true}', b"x" * (evidence.LIMIT + 1), b'{"success":true,"result":{}}', b'{"success":true,"errors":[1],"result":{}}'):
            api = self.client()
            api._client.open.side_effect = lambda request, timeout: Response(body, request.full_url)
            self.assertFalse(self.invoke(api)["acknowledged"])
        for attribute, value in (("status", 202), ("url", "https://foreign.invalid"), ("headers", {"Content-Encoding": "gzip"})):
            api = self.client()
            def received(request, timeout):
                response = Response(b'{}', request.full_url)
                setattr(response, attribute, value)
                return response
            api._client.open.side_effect = received
            self.assertFalse(self.invoke(api)["acknowledged"])
        api = self.client()
        response = mock.MagicMock()
        response.__enter__.side_effect = lambda: Response(json.dumps({"success": True, "errors": [], "result": self.raw()}).encode(), api._client.open.call_args.args[0].full_url)
        response.__exit__.side_effect = OSError("private-deploy-credential")
        api._client.open.side_effect = None
        api._client.open.return_value = response
        self.assertFalse(self.invoke(api)["acknowledged"])

    def test_readback_refuses_retired_or_foreign_deployments_and_changed_siblings(self):
        self.plan = self.populated()
        deployed = self.projected()
        after = dict(self.before, deployments=self.before["deployments"] + [deployed])
        for value in (self.before, dict(after, deployments=[deployed]), dict(after, previews=[self.preview]), dict(after, preview=dict(self.preview, id="recreated")), dict(after, deployments=after["deployments"] + [self.projected(2)])):
            with self.assertRaises(Refusal):
                apply.deployment_confirm(self.plan, value, deployed)
        with self.assertRaises(Refusal):
            apply.deployment_confirm(self.plan, after, self.projected(0))
        self.assertEqual(apply.deployment_confirm(self.plan, after, deployed), deployed)

    def test_private_credentials_and_completion_shape_refuse_before_network(self):
        for token in (None, "", "with space", "line\nbreak", "非ascii"):
            with self.assertRaises(Refusal):
                writer.Deployer(token, self.plan, test_cfworker_upload.FINAL)
        for completion in (None, "", "a.b.c", "非ascii", "a.b." + "c" * 8192):
            with self.assertRaises(Refusal):
                writer.Deployer("prepared", self.plan, completion)

    def test_parallel_calls_allow_one_network_attempt_and_transport_is_proxy_free(self):
        api = self.client()
        def attempt(number):
            try:
                return self.invoke(api)["acknowledged"]
            except Refusal:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(attempt, (0, 1))), [False, True])
        self.assertEqual(api._client.open.call_count, 1)
        with mock.patch("urllib.request.build_opener") as opened:
            writer.Deployer("prepared", self.plan, test_cfworker_upload.FINAL)
        handlers = opened.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaises(Refusal):
            handlers[1].redirect_request(None, None, 302, "redirect", {}, "https://foreign.invalid")
