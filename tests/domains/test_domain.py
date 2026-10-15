import copy
import http.client
import json
import unittest
import urllib.error
from unittest import mock

from lib.content.static import evidence
from lib.process.domains import identity, reader
from lib.refusal import Refusal
from tests.lib.process.test_cfworker import DEPLOYMENT, Response
from tests.lib.store.test_lane import operation, registered


def pins():
    return {"repository": "PerishLab/crest", "app": "crest", "account": "a" * 32,
            "worker": {"id": "parent-identity", "name": "crest-development"},
            "domain": {"id": "domain-identity", "hostname": "lane.crest.perish.uk",
                       "zone_id": "b" * 32, "zone_name": "perish.uk", "certificate_id": DEPLOYMENT}}


def reply():
    value = pins()["domain"]
    value["cert_id"] = value.pop("certificate_id")
    return dict(value, service="crest-development", enabled=False, previews_enabled=True)


class DomainIdentity(unittest.TestCase):
    def setUp(self):
        self.registered = registered(operation("review"))
        self.pins = pins()

    def test_exact_explicit_pins_are_detached_not_provider_authority(self):
        held = identity.prepare(self.registered, self.pins)
        self.assertEqual(held, self.pins)
        held["worker"]["id"] = "different"
        self.assertEqual(self.pins["worker"]["id"], "parent-identity")
        self.assertNotIn("enabled", held)

    def test_foreign_or_missing_app_account_parent_pins_refuse(self):
        for key, value in (("repository", "PerishLab/other"), ("app", "other"), ("account", "c" * 32),
                           ("worker", {"id": "parent-identity", "name": "other"})):
            with self.subTest(key=key), self.assertRaises(Refusal):
                identity.prepare(self.registered, dict(self.pins, **{key: value}))
        for key in self.pins:
            value = copy.deepcopy(self.pins)
            del value[key]
            self.assertRaises(Refusal, identity.prepare, self.registered, value)
        for value in (None, [], dict(self.pins, secret="private")):
            self.assertRaises(Refusal, identity.prepare, self.registered, value)

    def test_pinned_domain_zone_certificate_and_worker_shapes_refuse(self):
        for key, value in (("id", "../other"), ("id", ""), ("hostname", "lane.other.perish.uk"),
                           ("hostname", "LANE.crest.perish.uk"), ("hostname", "lane.crest.perish.uk."),
                           ("hostname", "https://lane.crest.perish.uk"), ("zone_name", "other.uk"),
                           ("zone_id", "b" * 31), ("certificate_id", "unverified")):
            held = copy.deepcopy(self.pins)
            held["domain"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                identity.prepare(self.registered, held)
        for held in (dict(self.pins, worker={"id": None, "name": "crest-development"}),
                     dict(self.pins, domain=dict(self.pins["domain"], arbitrary="private"))):
            self.assertRaises(Refusal, identity.prepare, self.registered, held)

    def test_registered_target_and_existing_adaptor_bounds_still_apply(self):
        for change in ({"enabled": False}, {"enabled": 1}, {"secret": "private"}):
            self.assertRaises(Refusal, identity.prepare, dict(self.registered, **change), self.pins)
        held = copy.deepcopy(self.registered)
        held["declaration"]["target"]["lane"] = "preview.review"
        self.assertRaises(Refusal, identity.prepare, held, self.pins)

    def test_exact_preview_only_observation_is_finite_and_detached(self):
        value = dict(reply(), annotations={"private": "hidden"})
        held = identity.observe(self.registered, self.pins, value)
        self.assertNotIn("annotations", held)
        held["id"] = "changed"
        self.assertEqual(value["id"], "domain-identity")
        self.assertEqual(self.pins, pins())

    def test_every_exact_identity_field_binds_and_missing_fields_refuse(self):
        for key in reply():
            value = reply()
            del value[key]
            self.assertRaises(Refusal, identity.observe, self.registered, self.pins, value)
            value = dict(reply(), **{key: "foreign"})
            self.assertRaises(Refusal, identity.observe, self.registered, self.pins, value)
        for value in (None, [], "unknown", {}):
            self.assertRaises(Refusal, identity.observe, self.registered, self.pins, value)

    def test_routing_flags_are_strict_booleans_never_sdk_defaults(self):
        for key, values in (("enabled", (True, None, 0, 1, "false")),
                            ("previews_enabled", (False, None, 0, 1, "true"))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                    identity.observe(self.registered, self.pins, dict(reply(), **{key: value}))

    def test_explicit_addresses_are_distinct_and_have_no_fallback(self):
        for name in ("a", "review", "preview"):
            held = identity.addresses(registered(operation(name)), self.pins, "7f201597")
            self.assertEqual(held, {"current": "https://" + name + ".lane.crest.perish.uk",
                                    "deployment": "https://7f201597-" + name + ".lane.crest.perish.uk"})
        held = registered(operation("a" * 48))
        held["mapping"]["resource"] = "crest"
        prepared = pins()
        prepared["worker"]["name"] = "crest"
        self.assertEqual(identity.addresses(held, prepared, "7f201597")["deployment"], "https://7f201597-" + "a" * 48 + ".lane.crest.perish.uk")
        for prefix in (None, "", True, "latest", DEPLOYMENT, "7F201597", "7f201597?x=1", "https://other.uk"):
            self.assertRaises(Refusal, identity.addresses, self.registered, self.pins, prefix)

    def test_dns_boundaries_refuse_instead_of_renaming(self):
        self.assertEqual(identity.hostname("a" * 63 + ".perish.uk"), "a" * 63 + ".perish.uk")
        for value in ("a" * 64 + ".perish.uk", "a." * 127 + "uk", "a..uk", ".uk", "a_.uk", "a.uk:443", "é.uk", None):
            self.assertRaises(Refusal, identity.hostname, value)


class DomainReader(unittest.TestCase):
    def client(self, body=None):
        api = reader.Reader("private-test-token", registered(operation("review")), pins())
        api._client = mock.Mock()
        encoded = body if body is not None else json.dumps({"success": True, "errors": [], "result": reply()}).encode()
        api._client.open.side_effect = lambda request, timeout: Response(encoded, request.full_url)
        return api

    def test_fixed_get_and_prepared_credentials_never_enter_projection(self):
        api = self.client()
        self.assertEqual(api.read(), reply())
        call = api._client.open.call_args
        request = call.args[0]
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/domains/domain-identity")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(call.kwargs, {"timeout": 30})
        self.assertEqual(request.get_header("Authorization"), "Bearer private-test-token")
        self.assertNotIn("private-test-token", repr(api))
        self.assertNotIn("private-test-token", json.dumps(api.read()))

    def test_input_pins_and_registration_are_copied_before_network(self):
        held, value = pins(), registered(operation("review"))
        api = reader.Reader("private-test-token", value, held)
        held["domain"]["id"] = "other"
        value["mapping"]["account"] = "c" * 32
        self.assertEqual(api._pins, pins())
        self.assertEqual(api._registered, registered(operation("review")))

    def test_invalid_credentials_and_pins_never_open_transport(self):
        with mock.patch.object(reader.urllib.request, "build_opener") as opener:
            for token in (None, "", "private\n", "private token", "é"):
                self.assertRaises(Refusal, reader.Reader, token, registered(operation()), pins())
            self.assertRaises(Refusal, reader.Reader, "private", registered(operation()), {})
            opener.assert_not_called()

    def test_malformed_duplicate_oversized_or_failed_replies_refuse(self):
        for body in (b"{}", b"[]", b"null", b"not-json", b'{"success":true,"success":true}',
                     b"x" * (evidence.LIMIT + 1),
                     json.dumps({"success": False, "errors": [], "result": reply()}).encode(),
                     json.dumps({"success": True, "result": reply()}).encode(),
                     json.dumps({"success": True, "errors": [{"private": "hidden"}], "result": reply()}).encode()):
            api = self.client(body)
            self.assertRaises(Refusal, api.read)
            self.assertEqual(api._client.open.call_count, 1)

    def test_http_redirect_transformation_and_wrong_endpoint_refuse(self):
        for change in ({"status": 201}, {"status": 404}, {"url": "https://other.invalid"},
                       {"headers": {"Content-Encoding": "gzip"}}):
            api = self.client()
            response = Response(json.dumps({"success": True, "errors": [], "result": reply()}).encode(), "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/domains/domain-identity")
            for key, value in change.items():
                setattr(response, key, value)
            api._client.open.side_effect = None
            api._client.open.return_value = response
            self.assertRaises(Refusal, api.read)
            self.assertEqual(api._client.open.call_count, 1)

    def test_transport_http_and_cleanup_errors_refuse_without_retry(self):
        for error in (OSError("private"), TimeoutError("private"), urllib.error.URLError("private"),
                      http.client.HTTPException("private"), urllib.error.HTTPError("https://private", 403, "private", {}, None)):
            api = self.client()
            api._client.open.side_effect = error
            with self.assertRaises(Refusal) as caught:
                api.read()
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(api._client.open.call_count, 1)
        api = self.client()
        response = mock.MagicMock()
        response.__enter__.side_effect = OSError("private")
        api._client.open.side_effect = None
        api._client.open.return_value = response
        self.assertRaises(Refusal, api.read)
        self.assertEqual(api._client.open.call_count, 1)

    def test_successful_body_cannot_mask_cleanup_failure(self):
        api = self.client()
        url = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/domains/domain-identity"
        response = Response(json.dumps({"success": True, "errors": [], "result": reply()}).encode(), url)
        context = mock.MagicMock()
        context.__enter__.return_value = response
        context.__exit__.side_effect = OSError("private")
        api._client.open.side_effect = None
        api._client.open.return_value = context
        self.assertRaises(Refusal, api.read)
        self.assertEqual(api._client.open.call_count, 1)
        response.close()
