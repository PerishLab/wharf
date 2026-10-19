import copy
import http.client
import json
import unittest
import urllib.error
from unittest import mock

from lib.content import resources
from lib.process.cfworker import observation
from lib.process.domains import base, baseline, context
from lib.process.lane import provider
from lib.refusal import Refusal
from tests.domains import fixtures
from tests.domains.test_base import DEPLOYMENT, VERSION
from tests.lib.process.test_cfworker import Response


class Baseline(unittest.TestCase):
    def setUp(self):
        self.fixture, self.api = fixtures.provider()
        self.opener = mock.patch.object(baseline.urllib.request, "build_opener", return_value=self.api._client)
        self.opener.start()
        self.addCleanup(self.opener.stop)

    def reader(self):
        return baseline.Reader("private-test-token", self.fixture.registered, self.fixture.policy)

    def test_exact_six_gets_bind_immutable_worker_and_full_base_ids(self):
        before = copy.deepcopy(self.fixture.policy)
        result = self.reader().read()
        self.assertEqual(base.isolated(before["base"], result), before["base"])
        account = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/"
        script = account + "scripts/crest-development"
        expected = [account + "workers/" + self.fixture.policy["id"] + "/versions/" + VERSION + "?include=modules",
                    script + "/deployments/" + DEPLOYMENT, script + "/deployments", script + "/routes",
                    account + "services/crest-development/environments/production/routes", script + "/schedules"]
        calls = [call.args[0] for call in self.api._client.open.call_args_list]
        self.assertEqual([item.full_url for item in calls], expected)
        self.assertTrue(all(item.get_method() == "GET" and item.data is None for item in calls))
        self.assertTrue(all(call.kwargs == {"timeout": 30} for call in self.api._client.open.call_args_list))
        self.assertNotIn("private-test-token", json.dumps(result))
        self.assertEqual(self.fixture.policy, before)

    def test_arbitrary_endpoint_and_missing_base_policy_refuse_before_io(self):
        for value in ("../secrets", "https://foreign.invalid", "latest", "", None, [], "version?x=1"):
            self.assertRaises(Refusal, self.reader()._get, value)
        for token in (None, "", "private\nvalue", "private value"):
            self.assertRaises(Refusal, baseline.Reader, token, self.fixture.registered, self.fixture.policy)
        for omitted in ("domain", "base"):
            policy = dict(self.fixture.policy)
            del policy[omitted]
            self.assertRaises(Refusal, baseline.Reader, "token", self.fixture.registered, policy)
        self.api._client.open.assert_not_called()

    def test_actual_optional_empty_version_shape_keeps_six_get_boundary(self):
        sample = resources.read_json("identity/fixtures/base-observation.json")["result"]
        before = copy.deepcopy(sample)
        self.fixture.policy["base"]["version"] = sample["id"]
        original = self.api._client.open.side_effect
        def received(request, timeout):
            response = original(request, timeout)
            value = json.load(response)
            if "/versions/" in request.full_url:
                value["result"] = sample
            elif "/deployments" in request.full_url:
                values = value["result"].get("deployments", [value["result"]])
                for item in values:
                    item["versions"][0]["version_id"] = sample["id"]
            return Response(json.dumps(value).encode(), request.full_url)
        self.api._client.open.side_effect = received
        result = self.reader().read()
        self.assertEqual(base.isolated(self.fixture.policy["base"], result), self.fixture.policy["base"])
        calls = [call.args[0] for call in self.api._client.open.call_args_list]
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(item.get_method() == "GET" and item.data is None for item in calls))
        self.assertTrue(calls[0].full_url.endswith(sample["id"] + "?include=modules"))
        self.assertEqual(sample, before)

    def test_proxies_and_redirects_are_disabled(self):
        with mock.patch.object(baseline.urllib.request, "build_opener") as opener:
            self.reader()
        handlers = opener.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], baseline.Redirect)
        self.assertRaises(Refusal, handlers[1].redirect_request, None)

    def test_each_failed_read_stops_without_retry_or_partial_qualification(self):
        original = self.api._client.open.side_effect
        for step in range(1, 7):
            self.api._client.reset_mock()
            seen = []
            def received(request, timeout):
                seen.append(request.full_url)
                if len(seen) == step:
                    raise urllib.error.HTTPError(request.full_url, 403, "private", {}, None)
                return original(request, timeout)
            self.api._client.open.side_effect = received
            self.assertRaises(Refusal, self.reader().read)
            self.assertEqual(len(seen), step)
        self.api._client.open.side_effect = original

    def test_route_and_schedule_unknown_nonempty_or_missing_values_refuse(self):
        for key in ("routes", "environment_routes", "schedules"):
            for value in (None, {}, ["foreign"], {"schedules": [], "extra": True}):
                supplied = dict(fixtures.baseline(), **{key: value})
                self.assertRaises(Refusal, base.isolated, self.fixture.policy["base"], supplied)
            supplied = fixtures.baseline()
            del supplied[key]
            self.assertRaises(Refusal, base.isolated, self.fixture.policy["base"], supplied)

    def test_malformed_envelopes_and_transformed_or_foreign_replies_refuse(self):
        url = "https://api.cloudflare.com/client/v4" + self.reader()._paths()["routes"]
        for value in ({}, {"success": True, "result": []}, {"success": 1, "errors": [], "result": []},
                      {"success": True, "errors": ["private"], "result": []}, {"success": True, "errors": [], "result": [], "result_info": {"page": 2}}):
            self.api._client.open.side_effect = None
            self.api._client.open.return_value = Response(json.dumps(value).encode(), url)
            self.assertRaises(Refusal, self.reader()._get, "routes")
        for body in (b'{"success":true,"success":true}', b"x" * 65537, b"{", b"[]"):
            self.api._client.open.return_value = Response(body, url)
            self.assertRaises(Refusal, self.reader()._get, "routes")
        for encoding in ("gzip", "br"):
            response = Response(b"{}", url)
            response.headers["Content-Encoding"] = encoding
            self.api._client.open.return_value = response
            self.assertRaises(Refusal, self.reader()._get, "routes")

    def test_base_changes_at_every_parent_observation_refuse(self):
        original = self.api._client.open.side_effect
        for step in range(1, 9):
            seen = []
            def received(request, timeout):
                response = original(request, timeout)
                if "/versions/" not in request.full_url:
                    return response
                seen.append(request.full_url)
                if len(seen) != step:
                    return response
                value = json.load(response)
                value["result"]["modules"][0]["content_base64"] = "Zm9yZWlnbg=="
                return Response(json.dumps(value).encode(), request.full_url)
            self.api._client.open.side_effect = received
            self.assertRaises(Refusal, self.api.observe, self.fixture.policy, "12345678-1234-1234-1234-123456789abc")
            self.assertEqual(len(seen), step)
        self.api._client.open.side_effect = original

    def test_deployment_current_routes_and_schedules_drift_refuses(self):
        original = self.api._client.open.side_effect
        suffixes = ("/deployments/" + DEPLOYMENT, "/deployments", "/scripts/crest-development/routes", "/environments/production/routes", "/schedules")
        for suffix in suffixes:
            for step in (1, 8):
                seen = []
                def received(request, timeout):
                    response = original(request, timeout)
                    if not request.full_url.endswith(suffix) or "/previews" in request.full_url:
                        return response
                    seen.append(request.full_url)
                    if len(seen) != step:
                        return response
                    value = json.load(response)
                    if "/routes" in suffix:
                        value["result"] = [{"pattern": "production.invalid/*"}]
                    elif suffix.endswith("/schedules"):
                        value["result"] = {"schedules": [{"cron": "* * * * *"}]}
                    elif suffix == "/deployments":
                        value["result"]["deployments"].insert(0, dict(value["result"]["deployments"][0], id=VERSION))
                    else:
                        value["result"]["versions"][0]["version_id"] = DEPLOYMENT
                    return Response(json.dumps(value).encode(), request.full_url)
                self.api._client.open.side_effect = received
                self.assertRaises(Refusal, self.api.observe, self.fixture.policy, "12345678-1234-1234-1234-123456789abc")
                self.assertEqual(len(seen), step)
        self.api._client.open.side_effect = original

    def test_transport_and_cleanup_failures_never_retry_or_expose_provider_text(self):
        for error in (OSError("private-test-token"), TimeoutError("private-test-token"),
                      urllib.error.URLError("private-test-token"), http.client.HTTPException("private-test-token")):
            self.api._client.open.reset_mock()
            self.api._client.open.side_effect = error
            with self.assertRaises(Refusal) as raised:
                self.reader().read()
            self.assertNotIn("private-test-token", str(raised.exception))
            self.assertEqual(self.api._client.open.call_count, 1)
        self.api._client.open.side_effect = None
        response = mock.MagicMock()
        url = "https://api.cloudflare.com/client/v4" + self.reader()._paths()["routes"]
        response.__enter__.return_value = Response(b'{"success":true,"errors":[],"result":[]}', url)
        response.__exit__.side_effect = OSError("private-test-token")
        self.api._client.open.return_value = response
        self.assertRaises(Refusal, self.reader()._get, "routes")

    def test_returned_version_urls_are_not_followed_or_isolation_evidence(self):
        original = self.api._client.open.side_effect
        def received(request, timeout):
            response = original(request, timeout)
            if "/versions/" not in request.full_url:
                return response
            value = json.load(response)
            value["result"]["urls"] = ["https://foreign.invalid/private"]
            return Response(json.dumps(value).encode(), request.full_url)
        self.api._client.open.side_effect = received
        self.assertEqual(self.api.status(self.fixture.policy)["parent"]["base"], self.fixture.policy["base"])
        self.assertTrue(all(call.args[0].full_url.startswith("https://api.cloudflare.com/client/v4/") for call in self.api._client.open.call_args_list))

    def test_missing_base_in_context_projection_never_falls_back(self):
        held = context.projected(self.fixture.policy, self.fixture.selected)
        self.assertEqual(held["base"], self.fixture.policy["base"])
        for key in ("domain", "base"):
            value = dict(held)
            del value[key]
            self.assertRaises(Refusal, context.restore, value, self.fixture.selected)
        self.assertRaises(Refusal, context.prepared, dict(self.fixture.policy, base=None), self.fixture.selected)
        native = {key: self.fixture.policy[key] for key in ("id", "subdomain")}
        self.assertRaises(Refusal, observation.parent, self.fixture.parent, self.fixture.selected, native)

    def test_trusted_runtime_base_pins_reject_missing_duplicate_foreign_and_unknown(self):
        env = {"WHARF_LANE_PARENT_ID": self.fixture.policy["id"], "WHARF_LANE_SUBDOMAIN": self.fixture.policy["subdomain"],
               "WHARF_LANE_DOMAIN_PINS": json.dumps(self.fixture.policy["domain"]), "WHARF_LANE_BASE_PINS": json.dumps(self.fixture.policy["base"])}
        self.assertEqual(provider.policy(env, self.fixture.registered), self.fixture.policy)
        for value in (None, "", "[]", "{", " " * 65537, '{"source":"a","source":"b"}',
                      json.dumps(dict(self.fixture.policy["base"], source="f" * 64)), json.dumps(dict(self.fixture.policy["base"], extra=True))):
            self.assertRaises(Refusal, provider.policy, dict(env, WHARF_LANE_BASE_PINS=value), self.fixture.registered)
        self.api._client.open.assert_not_called()
