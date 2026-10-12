import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from lib.content.static import evidence
from lib.process.cfworker import create, mapping, observation
from lib.refusal import Refusal
from tests.lib.process import test_cfworker_apply, test_cfworker_observation
from tests.lib.process.test_cfworker import Response


class Create(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_apply.Apply()
        fixture.setUp()
        self.fixture = fixture
        self.document, self.bodies = fixture.document, fixture.bodies
        provider = test_cfworker_observation.Observation()
        provider.setUp()
        self.policy = provider.policy
        self.parent = observation.parent(provider.parent, provider.selected, self.policy)
        self.before = {"parent": self.parent, "previews": [{"id": "sibling-id", "name": "sibling", "parent": self.parent["id"]}], "preview": None}
        self.plan = create.prepare(fixture.plan, self.before, self.policy)

    def raw(self, plan=None):
        name = mapping.selected((plan or self.plan)["apply"]["registration"])["name"]
        return {"id": "created-id", "name": name, "slug": name}

    def client(self, plan=None):
        api = create.Creator("private-create-credential", self.plan if plan is None else plan)
        api._client = mock.Mock()
        self.response(api, self.raw(plan))
        return api

    def response(self, api, value, status=200):
        body = json.dumps({"success": True, "errors": [], "result": value}).encode()
        def received(request, timeout):
            response = Response(body, request.full_url)
            response.status = status
            return response
        api._client.open.side_effect = received

    def invoke(self, api, observed=None):
        return api.create(self.before if observed is None else observed, self.document, self.bodies.__getitem__)

    def after(self, identity):
        return {"parent": self.parent, "previews": self.before["previews"] + [identity], "preview": identity}

    def test_default_and_two_keyed_creations_use_fixed_parent_without_inheritance(self):
        for lane in ("preview", "preview.design", "preview.review"):
            plan = create.prepare(self.fixture.prepared(lane), self.before, self.policy)
            api = self.client(plan)
            result = self.invoke(api)
            self.assertTrue(result["acknowledged"])
            request = api._client.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/workers/crest-development/previews?ignore_base_config=true")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(json.loads(request.data), {"name": lane.replace(".", "-")})
            self.assertEqual(request.get_header("Content-length"), str(len(request.data)))
            self.assertEqual(request.get_header("Authorization"), "Bearer private-create-credential")
            self.assertEqual(api._client.open.call_args.kwargs, {"timeout": 30})
            self.assertEqual(create.confirmed(plan, self.after(result["preview"]), result["preview"]), result["preview"])

    def test_creation_accepts_supported_object_statuses_but_never_settles_deploy(self):
        for status in (200, 201):
            api = self.client()
            self.response(api, self.raw(), status)
            result = self.invoke(api)
            self.assertEqual(set(result), {"request", "acknowledged", "preview"})
            self.assertTrue(result["acknowledged"])
            self.assertEqual(set(result["preview"]), {"id", "name", "parent"})
            self.assertNotIn("outcome", result)

    def test_finite_plan_and_acknowledgement_exclude_credentials_and_provider_fields(self):
        api = self.client()
        self.response(api, dict(self.raw(), env={"SECRET": "private-provider-value"}, urls=["https://untrusted.invalid"], annotations={"arbitrary": "not-proof"}))
        text = json.dumps([self.plan, self.invoke(api)])
        for private in ("private-create-credential", "private-provider-value", "untrusted.invalid", "not-proof"):
            self.assertNotIn(private, text)

    def test_quota_headroom_refuses_before_create_even_on_paid_accounts(self):
        before = copy.deepcopy(self.before)
        before["previews"] = [{"id": "id-" + str(number), "name": "sibling-" + str(number), "parent": self.parent["id"]} for number in range(98)]
        self.assertEqual(len(create.prepare(self.fixture.plan, before, self.policy)["before"]["previews"]), 98)
        for count in (99, 100, 500):
            before["previews"] = [{"id": "id-" + str(number), "name": "sibling-" + str(number), "parent": self.parent["id"]} for number in range(count)]
            with self.assertRaises(Refusal):
                create.prepare(self.fixture.plan, before, self.policy)

    def test_occupied_selection_and_malformed_catalog_refuse_before_custody(self):
        identity = {"id": "occupied-id", "name": "preview", "parent": self.parent["id"]}
        for before in (self.after(identity), dict(self.before, previews=self.before["previews"] * 2), dict(self.before, preview=identity), dict(self.before, parent=dict(self.parent, id="foreign"))):
            with self.assertRaises(Refusal):
                create.prepare(self.fixture.plan, before, self.policy)

    def test_unknown_disabled_stale_or_foreign_reservations_refuse(self):
        for field, value in (("phase", "unknown"), ("revision", 99), ("registration", "0" * 64)):
            plan = copy.deepcopy(self.plan)
            plan["apply"]["current"][field] = value
            with self.assertRaises(Refusal):
                self.client(plan)
        plan = copy.deepcopy(self.plan)
        plan["apply"]["registration"]["enabled"] = False
        with self.assertRaises(Refusal):
            self.client(plan)

    def test_plan_detaches_and_catalog_order_does_not_indicate_movement(self):
        plan = copy.deepcopy(self.plan)
        api = self.client(plan)
        plan["apply"]["registration"]["mapping"]["resource"] = "foreign"
        plan["before"]["previews"][0]["id"] = "foreign"
        observed = copy.deepcopy(self.before)
        observed["previews"].reverse()
        self.assertTrue(self.invoke(api, observed)["acknowledged"])

    def test_pre_write_parent_selection_and_sibling_drift_never_attempt_network(self):
        identity = {"id": "occupied-id", "name": "preview", "parent": self.parent["id"]}
        variants = [self.after(identity), dict(self.before, parent=dict(self.parent, id="foreign")), dict(self.before, previews=[]), dict(self.before, previews=[dict(self.before["previews"][0], id="recreated")])]
        for observed in variants:
            api = self.client()
            with self.assertRaises(Refusal):
                self.invoke(api, observed)
            api._client.open.assert_not_called()
            self.assertTrue(self.invoke(api)["acknowledged"])

    def test_handoff_byte_changes_refuse_before_consuming_attempt(self):
        api = self.client()
        key = self.document["files"]["index.html"]["blob"]
        held = self.bodies[key]
        self.bodies[key] = b"changed"
        with self.assertRaises(Refusal):
            self.invoke(api)
        api._client.open.assert_not_called()
        self.bodies[key] = held
        self.assertTrue(self.invoke(api)["acknowledged"])

    def test_success_and_response_loss_consume_the_only_attempt_without_leaking(self):
        for error in (None, TimeoutError("private-create-credential"), OSError("private-create-credential")):
            api = self.client()
            if error is not None:
                api._client.open.side_effect = error
            result = self.invoke(api)
            self.assertEqual(result["acknowledged"], error is None)
            self.assertNotIn("private-create-credential", json.dumps(result))
            if error is not None:
                self.assertIsNone(result["preview"])
            with self.assertRaises(Refusal):
                self.invoke(api)
            self.assertEqual(api._client.open.call_count, 1)

    def test_malformed_foreign_or_ambiguous_reply_identity_remains_unknown(self):
        for change in ({"id": "../id"}, {"id": "sibling-id"}, {"id": "sibling"}, {"id": "preview"}, {"name": "foreign"}, {"slug": "foreign"}, {"id": None}):
            api = self.client()
            self.response(api, dict(self.raw(), **change))
            self.assertFalse(self.invoke(api)["acknowledged"])
            with self.assertRaises(Refusal):
                self.invoke(api)

    def test_invalid_transformed_oversized_or_duplicate_replies_are_not_acknowledged(self):
        bodies = [b'{"success":true,"success":true,"errors":[],"result":{}}', b"x" * (evidence.LIMIT + 1), b'{"success":true,"result":{}}', b'{"success":true,"errors":[1],"result":{}}']
        for body in bodies:
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

    def test_cleanup_failure_is_unknown_even_after_reading_success(self):
        api = self.client()
        response = mock.MagicMock()
        response.__enter__.side_effect = lambda: Response(json.dumps({"success": True, "errors": [], "result": self.raw()}).encode(), api._client.open.call_args.args[0].full_url)
        response.__exit__.side_effect = OSError("private-create-credential")
        api._client.open.side_effect = None
        api._client.open.return_value = response
        self.assertFalse(self.invoke(api)["acknowledged"])
        with self.assertRaises(Refusal):
            self.invoke(api)

    def test_readback_refuses_missing_selected_identity_parent_or_sibling_changes(self):
        identity = {"id": "created-id", "name": "preview", "parent": self.parent["id"]}
        after = self.after(identity)
        variants = [self.before, dict(after, previews=[identity]), dict(after, parent=dict(self.parent, id="foreign")), dict(after, preview=dict(identity, id="recreated"))]
        for observed in variants:
            with self.assertRaises(Refusal):
                create.confirmed(self.plan, observed, identity)
        for change in ({"parent": "foreign"}, {"id": "sibling-id"}, {"name": "foreign"}, {"token": "never"}):
            with self.assertRaises(Refusal):
                create.confirmed(self.plan, after, dict(identity, **change))
        self.assertEqual(create.confirmed(self.plan, after, identity), identity)

    def test_private_malformed_or_oversized_plans_and_tokens_refuse(self):
        for value in (None, {}, dict(self.plan, token="never"), dict(self.plan, before="x" * 65537), dict(self.plan, policy=dict(self.policy, id="../parent"))):
            with self.assertRaises(Refusal):
                self.client(value if value is not None else {})
        for token in (None, "", "with space", "line\nbreak", "非ascii"):
            with self.assertRaises(Refusal):
                create.Creator(token, self.plan)

    def test_parallel_calls_allow_only_one_network_attempt(self):
        api = self.client()
        def attempt(number):
            try:
                return self.invoke(api)["acknowledged"]
            except Refusal:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(attempt, (0, 1))), [False, True])
        self.assertEqual(api._client.open.call_count, 1)

    def test_transport_is_proxy_free_and_refuses_redirects(self):
        with mock.patch("urllib.request.build_opener") as opened:
            create.Creator("prepared", self.plan)
        handlers = opened.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaises(Refusal):
            handlers[1].redirect_request(None, None, 302, "redirect", {}, "https://foreign.invalid")
