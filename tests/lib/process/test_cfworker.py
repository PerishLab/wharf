import copy
import io
import json
import unittest
import urllib.error
from unittest import mock

from lib.process.cfworker import mapping, reader
from lib.refusal import Refusal
from tests.lib.store.test_lane import operation, registered

DEPLOYMENT = "12345678-1234-1234-1234-123456789abc"


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, url):
        super().__init__(body)
        self.url = url
        self.headers = {}

    def geturl(self):
        return self.url


class StaticProvider(unittest.TestCase):
    def setUp(self):
        self.registered = registered(operation())

    def client(self, reply=None):
        api = reader.Reader("private-test-token", self.registered)
        api._client = mock.Mock()
        reply = reply or {"success": True, "errors": [], "result": {"name": "observed"}}
        api._client.open.side_effect = lambda request, timeout: Response(json.dumps(reply).encode(), request.full_url)
        return api

    def test_default_and_two_keyed_lanes_share_only_parent(self):
        values = [registered(operation(name)) for name in ("preview", "preview.review", "preview.design")]
        held = mapping.catalog(values)
        self.assertEqual([item["name"] for item in held], ["preview", "preview-review", "preview-design"])
        self.assertEqual({item["parent"] for item in held}, {"crest-development"})
        self.assertEqual([item["target"] for item in held], [item["declaration"]["target"] for item in values])

    def test_logical_and_physical_collisions_refuse(self):
        foreign = copy.deepcopy(self.registered)
        foreign["declaration"]["target"]["repository"] = "PerishLab/another"
        other = copy.deepcopy(self.registered)
        other["declaration"]["target"]["app"] = "another"
        for value in (self.registered, foreign, other):
            with self.subTest(value=value), self.assertRaises(Refusal):
                mapping.catalog([self.registered, value])

    def test_distinct_accounts_and_parents_do_not_collide(self):
        for key, value in (("account", "b" * 32), ("resource", "another-development")):
            other = copy.deepcopy(self.registered)
            other["declaration"]["target"]["app"] = "another"
            other["mapping"][key] = value
            self.assertEqual(len(mapping.catalog([self.registered, other])), 2)

    def test_length_bound_refuses_instead_of_truncating(self):
        self.registered["mapping"]["resource"] = "a" * 55
        self.assertEqual(len(mapping.selected(self.registered)["name"] + "-" + "a" * 55), 63)
        self.registered["mapping"]["resource"] = "a" * 56
        with self.assertRaises(Refusal):
            mapping.selected(self.registered)
        keyed = registered(operation("preview." + "a" * 48))
        with self.assertRaises(Refusal):
            mapping.selected(keyed)

    def test_registration_validation_and_detachment(self):
        held = mapping.selected(self.registered)
        held["target"]["app"] = "changed"
        self.assertEqual(self.registered["declaration"]["target"]["app"], "crest")
        for change in ({"enabled": False}, {"enabled": 1}, {"secret": "never"}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                mapping.selected(dict(self.registered, **change))
        for value in (None, [], {}, [None]):
            with self.assertRaises(Refusal):
                mapping.catalog(value)

    def test_fixed_authenticated_routing_is_not_product_config(self):
        api = self.client()
        for invoke in (api.parent, api.preview, lambda: api.deployment(DEPLOYMENT)):
            self.assertEqual(invoke(), {"name": "observed"})
        calls = api._client.open.call_args_list
        base = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/workers/crest-development"
        self.assertEqual([call.args[0].full_url for call in calls], [base, base + "/previews/preview", base + "/previews/preview/deployments/" + DEPLOYMENT])
        for call in calls:
            self.assertEqual(call.args[0].get_method(), "GET")
            self.assertIsNone(call.args[0].data)
            self.assertEqual(call.kwargs, {"timeout": 30})
        self.assertNotIn("private-test-token", repr(api))

    def test_unsafe_or_mutable_selectors_never_reach_network(self):
        api = self.client()
        for suffix in (None, True, [], "/previews/another", "/previews/preview?token=private", "/previews/preview/../production", "/versions", "/previews/preview/deployments/latest", "https://attacker.invalid"):
            with self.subTest(suffix=suffix), self.assertRaises(Refusal):
                api.get(suffix)
        for identity in (None, True, "latest", DEPLOYMENT.upper(), DEPLOYMENT + "?query", "../parent"):
            with self.assertRaises(Refusal):
                api.deployment(identity)
        api._client.open.assert_not_called()

    def test_failure_is_not_absence_and_never_retries(self):
        for error in (urllib.error.HTTPError("private-test-token", 404, "private-test-token", {}, None), urllib.error.URLError("private-test-token"), TimeoutError("private-test-token"), OSError("private-test-token")):
            api = self.client()
            api._client.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(Refusal) as raised:
                api.parent()
            self.assertNotIn("private-test-token", str(raised.exception))
            self.assertEqual(api._client.open.call_count, 1)

    def test_redirect_transform_wrong_status_and_cleanup_refuse(self):
        with self.assertRaises(Refusal):
            reader.Redirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid")
        for field, value in (("url", "https://attacker.invalid"), ("status", 404), ("headers", {"Content-Encoding": "gzip"})):
            api = self.client()
            response = Response(b'{}', "unused")
            setattr(response, field, value)
            api._client.open.side_effect = None
            api._client.open.return_value = response
            with self.assertRaises(Refusal):
                api.parent()
        api = self.client()
        response = mock.MagicMock()
        response.__exit__.side_effect = OSError("private-test-token")
        api._client.open.side_effect = None
        api._client.open.return_value = response
        with self.assertRaises(Refusal):
            api.parent()

    def test_duplicate_malformed_oversize_or_unsuccessful_replies_refuse(self):
        bodies = [b'{}', b'[]', b'null', b'bad', b'\xff', b'{"success":true,"success":false}', b'x' * (1048576 + 1)]
        bodies.extend(json.dumps(value).encode() for value in ({"success": 1, "errors": [], "result": {}}, {"success": True, "errors": ["denied"], "result": {}}, {"success": True, "errors": [], "result": []}))
        for body in bodies:
            api = self.client()
            api._client.open.side_effect = lambda request, timeout: Response(body, request.full_url)
            with self.subTest(body=body[:40]), self.assertRaises(Refusal):
                api.preview()

    def test_no_proxy_and_prepared_token_only(self):
        for token in (None, "", "with space", "line\nbreak", "\x7f", "非ascii"):
            with self.assertRaises(Refusal):
                reader.Reader(token, self.registered)
        with mock.patch.object(reader.urllib.request, "build_opener") as factory:
            reader.Reader("private-test-token", self.registered)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], reader.Redirect)


if __name__ == "__main__":
    unittest.main()
