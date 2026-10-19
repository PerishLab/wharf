import copy
import json
import unittest
import urllib.error
from unittest import mock

from lib.process.cfworker import mapping
from lib.process.domains import context, reader, routing
from lib.refusal import Refusal
from tests.domains import fixtures
from tests.lib.process.test_cfworker import DEPLOYMENT, Response


class Runtime(unittest.TestCase):
    def setUp(self):
        self.fixture, self.api = fixtures.provider()
        self.opener = mock.patch.object(reader.urllib.request, "build_opener", return_value=self.api._client)
        self.opener.start()
        self.addCleanup(self.opener.stop)

    def test_exact_domain_gets_surround_complete_native_routing_observation(self):
        held = self.api.observe(self.fixture.policy, DEPLOYMENT)
        self.assertEqual(held["status"]["parent"]["domain"], self.fixture.policy["domain"])
        self.assertEqual(routing.checked(held, held["status"], held["deployment"]), "https://12345678.lane.crest.perish.uk")
        calls = [call.args[0] for call in self.api._client.open.call_args_list]
        domains = [request for request in calls if "/workers/domains/" in request.full_url]
        self.assertEqual(len(calls), 86)
        self.assertEqual(len(domains), 8)
        self.assertTrue(all(request.full_url == "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/domains/domain-identity" for request in domains))
        self.assertTrue(all(request.get_method() == "GET" and request.data is None for request in calls))
        self.assertNotIn("private-test-token", json.dumps(held))
        self.fixture.policy["domain"]["domain"]["id"] = "changed"
        self.assertEqual(held["status"]["parent"]["domain"]["domain"]["id"], "domain-identity")

    def protocol(self, transform):
        original = self.api._client.open.side_effect
        def received(request, timeout):
            with original(request, timeout) as response:
                value = json.load(response)
            transform(value, request.full_url)
            return Response(json.dumps(value).encode(), request.full_url)
        self.api._client.open.side_effect = received

    def test_domain_movement_at_each_independent_get_never_returns_a_projection(self):
        original = self.api._client.open.side_effect
        for step in range(1, 9):
            self.api._client.reset_mock()
            seen = []
            def received(request, timeout):
                if "/workers/domains/" in request.full_url:
                    seen.append(request.full_url)
                    if len(seen) == step:
                        raise urllib.error.HTTPError(request.full_url, 404, "private", {}, None)
                return original(request, timeout)
            self.api._client.open.side_effect = received
            with self.subTest(step=step), self.assertRaises(Refusal):
                self.api.observe(self.fixture.policy, DEPLOYMENT)
            self.assertEqual(len(seen), step)

    def test_native_only_domain_only_foreign_duplicate_and_full_uuid_urls_refuse(self):
        original = self.api._client.open.side_effect
        for mode in ("native-only", "domain-only", "foreign", "duplicate", "uuid"):
            self.api._client.open.side_effect = original
            def transform(value, url):
                entries = value["result"] if isinstance(value["result"], list) else [value["result"]]
                for item in entries:
                    if not item.get("urls"):
                        continue
                    urls = item["urls"]
                    if mode in ("native-only", "domain-only"):
                        item["urls"] = urls[:1] if mode == "native-only" else urls[1:]
                    elif mode == "foreign":
                        urls[1] = "https://foreign.invalid"
                    elif mode == "duplicate":
                        urls[1] = urls[0]
                    elif "/deployments" in url:
                        urls[1] = urls[1].replace(DEPLOYMENT[:8], DEPLOYMENT)
            self.protocol(transform)
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                self.api.observe(self.fixture.policy, DEPLOYMENT)

    def test_url_order_is_not_identity_and_undeployed_preview_may_omit_urls(self):
        self.protocol(lambda value, url: [item["urls"].reverse() for item in (value["result"] if isinstance(value["result"], list) else [value["result"]]) if item.get("urls")])
        self.assertEqual(self.api.observe(self.fixture.policy, DEPLOYMENT)["deployment"]["id"], DEPLOYMENT)
        held = context.projected(self.fixture.policy, self.fixture.selected)
        value = dict(self.fixture.preview, urls=[])
        self.assertEqual(context.resource(value, self.fixture.selected, held)["id"], "preview-id")
        self.assertRaises(Refusal, context.preview, value, self.fixture.selected, held)

    def test_missing_invalid_or_foreign_explicit_pins_refuse_before_io(self):
        for value in (None, {}, dict(self.fixture.policy, domain=None), dict(self.fixture.policy, extra=True)):
            self.assertRaises(Refusal, self.api.status, value)
        for key in self.fixture.policy["domain"]:
            value = copy.deepcopy(self.fixture.policy)
            value["domain"][key] = "foreign"
            self.assertRaises(Refusal, self.api.status, value)
        self.api._client.open.assert_not_called()

    def test_parent_reference_and_independent_domain_flags_cannot_disagree(self):
        original = self.api._client.open.side_effect
        for mode in ("production", "missing-reference", "foreign-certificate", "foreign-parent"):
            self.api._client.open.side_effect = original
            def transform(value, url):
                if "/workers/domains/" in url and mode == "production":
                    value["result"]["enabled"] = True
                if url.endswith("/workers/crest-development"):
                    if mode == "missing-reference":
                        value["result"]["references"]["domains"] = []
                    elif mode == "foreign-certificate":
                        value["result"]["references"]["domains"][0]["certificate_id"] = "foreign"
                    elif mode == "foreign-parent":
                        value["result"]["id"] = "foreign"
            self.protocol(transform)
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                self.api.status(self.fixture.policy)

    def test_projection_context_cannot_substitute_account_app_or_parent(self):
        held = context.projected(self.fixture.policy, mapping.selected(self.fixture.registered))
        for key, value in (("account", "f" * 32), ("name", "foreign"), ("id", "foreign"), ("suffix", "-other.foreign.workers.dev")):
            self.assertRaises(Refusal, context.restore, dict(held, **{key: value}), self.fixture.selected)
        for domain in (None, {}, dict(held["domain"], app=None)):
            self.assertRaises(Refusal, context.current, dict(held, domain=domain), "preview")

    def test_supplied_domain_observation_cannot_replace_prepared_subdomain_policy(self):
        value = {"policy": {"id": self.fixture.policy["id"], "subdomain": "foreign", "base": self.fixture.policy["base"]},
                 "parent": copy.deepcopy(self.fixture.parent), "domain": fixtures.reply(), "base": fixtures.baseline()}
        value["parent"]["subdomain"]["preview_url_suffix"] = "-crest-development.foreign.workers.dev"
        self.assertRaises(Refusal, context.observed, self.fixture.registered, self.fixture.policy, value)

    def test_native_namespace_names_are_not_rewritten_as_logical_lane_aliases(self):
        held = context.projected(self.fixture.policy, self.fixture.selected)
        self.assertEqual(context.current(held, "preview-design"), "https://preview-design.lane.crest.perish.uk")
        self.assertEqual(context.address(held, "preview-design", DEPLOYMENT, DEPLOYMENT[:8]),
                         "https://12345678.lane.crest.perish.uk")
        for name in (None, "", "foreign/path", "a" * 64):
            self.assertRaises(Refusal, context.current, held, name)
