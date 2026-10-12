import copy
import hashlib
import http.client
import json
import unittest
import urllib.error
from email.message import Message
from pathlib import PurePosixPath
from unittest import mock

from lib.content.static import assets
from lib.process.cfworker import apply, payload, reader
from lib.refusal import Refusal
from lib.store import handoff
from lib.store.lane import state
from tests.cfworker import test_deployment
from tests.lib.process.test_cfworker import Response


class Readback(unittest.TestCase):
    def setUp(self):
        self.fixture = test_deployment.Deployment()
        self.fixture.setUp()
        self.plan = self.fixture.plan
        self.deployed = self.fixture.projected()
        self.after = dict(self.fixture.before, deployments=[self.deployed])
        self.origin = "https://" + self.deployed["id"] + self.fixture.parent["suffix"]

    def client(self):
        api = reader.Readback(self.plan, self.after, self.deployed)
        api._client = mock.Mock()
        api._client.open.side_effect = self.received
        return api

    def received(self, request, timeout):
        routes = payload.routes(self.fixture.fixture.files)
        name, body = routes[request.full_url.removeprefix(self.origin)]
        response = Response(body, request.full_url)
        response.headers = Message()
        headers = {"Content-Security-Policy": assets.CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                   "X-Robots-Tag": "noindex, nofollow", "Content-Type": payload.MIME[PurePosixPath(name).suffix.lower()], "Content-Length": str(len(body))}
        for key, value in headers.items():
            response.headers[key] = value
        return response

    def invoke(self, api):
        return api.read(self.fixture.document, self.fixture.bodies.__getitem__)

    def changed(self, files):
        fixture = self.fixture.fixture
        fixture.document, fixture.files = handoff.describe(files, fixture.receipt)
        fixture.bodies = {hashlib.sha256(body).hexdigest(): body for body in fixture.files.values()}
        fixture.plan = fixture.prepared()
        self.fixture.document, self.fixture.bodies = fixture.document, fixture.bodies
        self.plan = apply.deployment_prepare(fixture.plan, self.fixture.before, self.fixture.policy)

    def test_default_and_two_keyed_lanes_read_every_exact_asset_without_credentials(self):
        for lane in ("preview", "preview.design", "preview.review"):
            self.fixture.preview["name"] = lane.replace(".", "-")
            plan = self.fixture.fixture.prepared(lane)
            self.plan = apply.deployment_prepare(plan, self.fixture.before, self.fixture.policy)
            api = self.client()
            result = self.invoke(api)
            calls = api._client.open.call_args_list
            self.assertEqual([call.args[0].full_url for call in calls], [self.origin + route for route in payload.routes(self.fixture.fixture.files)])
            self.assertEqual(result["artifact"], plan["artifact"])
            self.assertEqual(result["deployment"], self.deployed)
            self.assertEqual(result["address"], self.origin)
            self.assertEqual(result["files"], len(self.fixture.fixture.files) - 1)
            for call in calls:
                request = call.args[0]
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)
                self.assertEqual(request.header_items(), [("Accept-encoding", "identity"), ("Cache-control", "no-cache"), ("User-agent", "wharf-lane")])
                self.assertEqual(call.kwargs, {"timeout": 30})
            self.assertIn(self.origin + "/" + assets.IDENTITY, [call.args[0].full_url for call in calls])
            self.assertNotIn("outcome", result)
            self.assertNotIn("writer", result)
            result["deployment"]["id"] = "changed"
            self.assertEqual(self.invoke(api)["deployment"], self.deployed)

    def test_canonical_html_nested_assets_and_all_fixed_media_types(self):
        files = {"index.html": b"<h1>Root</h1>", "about.html": b"<h1>About</h1>", "docs/index.html": b"<h1>Docs</h1>",
                 "docs/other.html": b"<h1>Other</h1>", "404.html": b"<h1>Missing</h1>", "style.css": b"h1{color:red}",
                 "logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>', "image.png": b"\x89PNG\r\n\x1a\nbytes",
                 "photo.jpg": b"\xff\xd8\xffbytes", "other.jpeg": b"\xff\xd8\xffbytes", "favicon.ico": b"\x00\x00\x01\x00bytes", "font.woff": b"wOFFbytes", "font.woff2": b"wOF2bytes"}
        self.changed(files)
        api = self.client()
        self.invoke(api)
        routes = [call.args[0].full_url.removeprefix(self.origin) for call in api._client.open.call_args_list]
        for path in ("/", "/about", "/docs/", "/docs/other", "/404", "/" + assets.IDENTITY):
            self.assertIn(path, routes)
        self.assertFalse(any(path.endswith(".html") or path == "/_headers" for path in routes))

    def test_ambiguous_or_unsupported_routes_refuse_before_network(self):
        for extra in ({"page.html": b"<p>A</p>", "page/index.html": b"<p>B</p>"}, {"page.html.html": b"<p>A</p>"}, {"index/index.html": b"<p>A</p>"}):
            self.changed(dict(extra, **{"index.html": b"<p>Root</p>"}))
            api = self.client()
            with self.assertRaises(Refusal):
                self.invoke(api)
            api._client.open.assert_not_called()

    def test_exact_handoff_is_revalidated_before_and_after_reads(self):
        api = self.client()
        key = self.fixture.document["files"]["index.html"]["blob"]
        self.fixture.bodies[key] = b"changed"
        with self.assertRaises(Refusal):
            self.invoke(api)
        api._client.open.assert_not_called()
        self.setUp()
        api = self.client()
        def moving(request, timeout):
            response = self.received(request, timeout)
            self.fixture.bodies[self.fixture.document["files"]["index.html"]["blob"]] = b"changed"
            return response
        api._client.open.side_effect = moving
        with self.assertRaises(Refusal):
            self.invoke(api)

    def test_stale_unknown_unconfirmed_foreign_and_private_plans_refuse(self):
        for change in (dict(self.plan, token="private"), dict(self.plan, before={})):
            with self.assertRaises(Refusal):
                reader.Readback(change, self.after, self.deployed)
        plan = copy.deepcopy(self.plan)
        plan["apply"]["current"]["phase"] = "unknown"
        with self.assertRaises(Refusal):
            reader.Readback(plan, self.after, self.deployed)
        for observed in (self.fixture.before, dict(self.after, deployments=[])):
            with self.assertRaises(Refusal):
                reader.Readback(self.plan, observed, self.deployed)
        for deployed in (dict(self.deployed, id="latest"), dict(self.deployed, preview="foreign"), dict(self.deployed, url="https://foreign.invalid")):
            with self.assertRaises(Refusal):
                reader.Readback(self.plan, self.after, deployed)

    def test_prepared_address_dns_limit_refuses_without_truncation(self):
        plan = copy.deepcopy(self.plan)
        plan["apply"]["registration"]["mapping"]["resource"] = "a" * 27
        registered = plan["apply"]["registration"]
        active = {key: plan["apply"]["current"]["active"][key] for key in ("request", "assessment")}
        plan["apply"]["current"] = state.started(state.initial(registered), active, registered)
        plan["before"]["parent"]["name"] = "a" * 27
        plan["before"]["parent"]["suffix"] = "-" + "a" * 27 + "." + plan["policy"]["subdomain"] + ".workers.dev"
        after = dict(plan["before"], deployments=[self.deployed])
        with self.assertRaisesRegex(Refusal, "DNS"):
            reader.Readback(plan, after, self.deployed)

    def test_redirect_error_partial_and_wrong_origin_refuse(self):
        for key, value in (("status", 307), ("status", 404), ("status", 206), ("url", "https://foreign.invalid"), ("url", "https://preview-crest-development.test.workers.dev/")):
            api = self.client()
            def bad(request, timeout):
                response = self.received(request, timeout)
                setattr(response, key, value)
                return response
            api._client.open.side_effect = bad
            with self.assertRaises(Refusal):
                self.invoke(api)
            self.assertEqual(api._client.open.call_count, 1)

    def test_security_headers_are_required_and_duplicate_values_refuse(self):
        for name in ("Content-Security-Policy", "X-Content-Type-Options", "Cache-Control", "X-Robots-Tag", "Content-Type"):
            for mode in ("absent", "changed", "duplicate"):
                api = self.client()
                def bad(request, timeout):
                    response = self.received(request, timeout)
                    held = response.headers[name]
                    if mode != "duplicate":
                        del response.headers[name]
                    if mode != "absent":
                        response.headers[name] = "private-wrong-value" if mode == "changed" else held
                    return response
                api._client.open.side_effect = bad
                if name == "X-Robots-Tag" and mode == "duplicate":
                    self.invoke(api)
                else:
                    with self.assertRaises(Refusal):
                        self.invoke(api)

    def test_transformed_ranged_length_mismatch_and_wrong_charset_refuse(self):
        for name, value in (("Content-Encoding", "gzip"), ("Content-Range", "bytes 0-1/3"), ("Content-Length", "999"), ("Content-Type", "text/html; charset=utf-16"), ("X-Robots-Tag", "noindex, nofollow, all")):
            api = self.client()
            def bad(request, timeout):
                response = self.received(request, timeout)
                del response.headers[name]
                response.headers[name] = value
                return response
            api._client.open.side_effect = bad
            with self.assertRaises(Refusal):
                self.invoke(api)

    def test_truncated_changed_and_oversized_bytes_never_prove_content(self):
        for transform in (lambda body: body[:-1], lambda body: b"x" * len(body), lambda body: body + b"x"):
            api = self.client()
            def bad(request, timeout):
                response = self.received(request, timeout)
                response.seek(0)
                body = response.read()
                response.seek(0)
                response.truncate()
                response.write(transform(body))
                response.seek(0)
                return response
            api._client.open.side_effect = bad
            with self.assertRaises(Refusal):
                self.invoke(api)

    def test_network_and_cleanup_failures_refuse_without_retry_or_private_output(self):
        for error in (OSError("private-value"), TimeoutError("private-value"), urllib.error.URLError("private-value"), http.client.IncompleteRead(b"private-value")):
            api = self.client()
            api._client.open.side_effect = error
            with self.assertRaises(Refusal) as raised:
                self.invoke(api)
            self.assertNotIn("private-value", str(raised.exception))
            self.assertEqual(api._client.open.call_count, 1)
        api = self.client()
        def bad(request, timeout):
            response = mock.MagicMock()
            response.__enter__.return_value = self.received(request, timeout)
            response.__exit__.side_effect = OSError("private-value")
            return response
        api._client.open.side_effect = bad
        with self.assertRaises(Refusal):
            self.invoke(api)

    def test_failure_at_any_asset_never_returns_partial_success(self):
        for position in range(len(self.fixture.fixture.files) - 1):
            api = self.client()
            def bad(request, timeout):
                if api._client.open.call_count == position + 1:
                    raise TimeoutError("private-value")
                return self.received(request, timeout)
            api._client.open.side_effect = bad
            with self.assertRaises(Refusal):
                self.invoke(api)
            self.assertEqual(api._client.open.call_count, position + 1)

    def test_utf8_and_provider_noindex_are_admitted_without_length_header(self):
        api = self.client()
        def compatible(request, timeout):
            response = self.received(request, timeout)
            kind = response.headers["Content-Type"]
            del response.headers["Content-Type"]
            del response.headers["Content-Length"]
            response.headers["Content-Type"] = kind + "; charset=UTF-8"
            response.headers["X-Robots-Tag"] = "noindex"
            response.headers["Content-Encoding"] = "identity"
            return response
        api._client.open.side_effect = compatible
        self.assertEqual(self.invoke(api)["address"], self.origin)

    def test_transport_is_proxy_free_redirect_refusing_and_has_no_credentials(self):
        with mock.patch("urllib.request.build_opener") as build:
            api = reader.Readback(self.plan, self.after, self.deployed)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], reader.Redirect)
        self.assertFalse(hasattr(api, "_token"))
        with self.assertRaises(Refusal):
            reader.Redirect().redirect_request(None, None, 302, "moved", {}, "https://foreign.invalid")
        self.assertNotIn("outcome", json.dumps(self.invoke(self.client())))
