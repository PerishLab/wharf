import base64
import copy
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from unittest import mock

from lib.content.static import assets, evidence
from lib.process.cfworker import payload, upload
from lib.refusal import Refusal
from tests.lib.process.test_cfworker import Response
from tests.lib.process import test_cfworker_apply

JWT = "header." + base64.urlsafe_b64encode(b'{}').decode().rstrip("=") + ".private"
FINAL = JWT + "completion"


class Upload(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_apply.Apply()
        fixture.setUp()
        self.fixture = fixture
        self.plan, self.document, self.bodies = fixture.plan, fixture.document, fixture.bodies
        self.hashes = list(dict.fromkeys(item["hash"] for item in payload.manifest(self.plan, self.document, self.bodies.__getitem__).values()))

    def client(self, groups=None):
        api = upload.Uploader("private-session-credential", self.plan)
        api._client = mock.Mock()
        groups = [self.hashes] if groups is None else groups
        reply = {"success": True, "errors": None, "result": {"jwt": JWT, "buckets": groups}}
        api._client.open.side_effect = lambda request, timeout: Response(json.dumps(reply).encode(), request.full_url)
        return api

    def session(self, api):
        return api.session(self.document, self.bodies.__getitem__)

    def bucket(self, api, number=0):
        return api.bucket(number, self.document, self.bodies.__getitem__)

    def response(self, api, result, status=201):
        body = json.dumps({"success": True, "errors": [], "result": result}).encode()
        def received(request, timeout):
            reply = Response(body, request.full_url)
            reply.status = status
            return reply
        api._client.open.side_effect = received

    def test_default_and_two_keyed_sessions_use_only_parent_asset_endpoint(self):
        for lane in ("preview", "preview.design", "preview.review"):
            self.plan = self.fixture.prepared(lane)
            api = self.client()
            result = self.session(api)
            request = api._client.open.call_args.args[0]
            self.assertTrue(result["acknowledged"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["buckets"], 1)
            self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/scripts/crest-development/assets-upload-session")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer private-session-credential")
            self.assertEqual(json.loads(request.data), {"manifest": payload.manifest(self.plan, self.document, self.bodies.__getitem__)})
            self.assertEqual(api._client.open.call_args.kwargs, {"timeout": 30})

    def test_bulk_multipart_transfers_exact_base64_bytes_with_fixed_media_types(self):
        api = self.client()
        self.session(api)
        self.response(api, {"jwt": FINAL})
        result = self.bucket(api)
        request = api._client.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/assets/upload?base64=true")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + JWT)
        self.assertEqual(request.get_header("Content-length"), str(len(request.data)))
        message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + request.get_header("Content-type") + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + request.data)
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), len(self.hashes))
        manifest = payload.manifest(self.plan, self.document, self.bodies.__getitem__)
        files = self.fixture.files
        for part in parts:
            held = part.get_param("name", header="content-disposition")
            name = next(name[1:] for name, entry in manifest.items() if entry["hash"] == held)
            self.assertEqual(part.get_filename(), held)
            self.assertEqual(base64.b64decode(part.get_payload(decode=True), validate=True), files[name])
            self.assertEqual(part.get_content_type(), upload.MIME[upload.PurePosixPath(name).suffix.lower()])
        self.assertNotIn("/_headers", manifest)
        self.assertTrue(result["complete"])
        self.assertEqual(api.completion(self.document, self.bodies.__getitem__), FINAL)

    def test_cached_manifest_returns_private_completion_without_upload(self):
        api = self.client([])
        result = self.session(api)
        self.assertTrue(result["complete"])
        self.assertEqual(api.completion(self.document, self.bodies.__getitem__), JWT)
        with self.assertRaises(Refusal):
            self.bucket(api)
        self.assertEqual(api._client.open.call_count, 1)

    def test_workflow_schedules_each_bucket_once_in_any_order(self):
        api = self.client([[self.hashes[0]], self.hashes[1:]])
        self.session(api)
        self.response(api, {}, 200)
        self.assertFalse(self.bucket(api, 1)["complete"])
        with self.assertRaises(Refusal):
            self.bucket(api, 1)
        with self.assertRaises(Refusal):
            api.completion(self.document, self.bodies.__getitem__)
        self.response(api, {"jwt": FINAL})
        self.assertTrue(self.bucket(api, 0)["complete"])

    def test_finite_results_and_plans_exclude_private_credentials(self):
        api = self.client([])
        observed = json.dumps([self.session(api), self.plan])
        for private in (JWT, FINAL, "private-session-credential"):
            self.assertNotIn(private, observed)
        self.assertEqual(set(api.result(True)), {"request", "acknowledged", "buckets", "complete"})

    def test_entire_foreign_repeated_or_malformed_catalog_blocks_all_uploads(self):
        variants = [None, {}, [[]], [["f" * 32]], [[self.hashes[0]], [self.hashes[0]]], [[False]], [self.hashes] * (len(self.hashes) + 1)]
        for groups in variants:
            api = self.client()
            self.response(api, {"jwt": JWT, "buckets": groups}, 200)
            self.assertFalse(self.session(api)["acknowledged"])
            with self.assertRaises(Refusal):
                self.bucket(api)
            self.assertEqual(api._client.open.call_count, 1)

    def test_lost_session_reply_is_consumed_without_retry_or_token_leak(self):
        api = self.client()
        api._client.open.side_effect = TimeoutError("private-session-credential")
        self.assertFalse(self.session(api)["acknowledged"])
        with self.assertRaises(Refusal):
            self.session(api)
        with self.assertRaises(Refusal):
            self.bucket(api)
        self.assertEqual(api._client.open.call_count, 1)

    def test_unknown_diagnostics_are_finite_safe_and_never_enable_continuation(self):
        secret = JWT + '\n::error::private-session-credential'
        cases = [(TimeoutError(secret), 'timeout'), (OSError(secret), 'transport'), (Refusal(secret), 'protocol'), (upload.urllib.error.HTTPError('https://private.invalid', 403, secret, {}, io.BytesIO(secret.encode())), 'http'), (upload.urllib.error.HTTPError(secret, secret, secret, {}, io.BytesIO(secret.encode())), 'http')]
        for phase in ('session', 'bucket'):
            for error, category in cases:
                api = self.client()
                if phase == 'bucket':
                    self.session(api)
                api._client.open.side_effect = error
                with mock.patch('sys.stderr', new_callable=io.StringIO) as stream:
                    result = self.session(api) if phase == 'session' else self.bucket(api)
                held = json.loads(stream.getvalue())
                self.assertEqual(held, {'schema': 'wharf.cfworker.diagnostic/v1', 'stage': phase + '-response', 'category': category, 'status': 403 if category == 'http' and error.code == 403 else None})
                self.assertFalse(result['acknowledged'])
                self.assertEqual(set(result), {'request', 'acknowledged', 'buckets', 'complete'})
                with self.assertRaises(Refusal):
                    self.session(api) if phase == 'session' else self.bucket(api)
                with self.assertRaises(Refusal):
                    api.completion(self.document, self.bodies.__getitem__)
                self.assertEqual(api._client.open.call_count, 1 if phase == 'session' else 2)

    def test_diagnostic_rejects_foreign_stage_without_printing_it(self):
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stream, self.assertRaises(Refusal):
            upload.diagnostic(JWT, Refusal(JWT))
        self.assertEqual(stream.getvalue(), '')

    def test_lost_bucket_reply_blocks_other_buckets_and_completion(self):
        api = self.client([[self.hashes[0]], self.hashes[1:]])
        self.session(api)
        api._client.open.side_effect = OSError(JWT)
        self.assertFalse(self.bucket(api)["acknowledged"])
        for number in (0, 1):
            with self.assertRaises(Refusal):
                self.bucket(api, number)
        with self.assertRaises(Refusal):
            api.completion(self.document, self.bodies.__getitem__)
        self.assertEqual(api._client.open.call_count, 2)

    def test_early_missing_malformed_or_wrong_status_completion_blocks(self):
        for result, status in (({}, 200), ({"jwt": FINAL}, 200), ({"jwt": "bad"}, 201), ({"jwt": FINAL, "extra": 1}, 201)):
            api = self.client()
            self.session(api)
            self.response(api, result, status)
            self.assertFalse(self.bucket(api)["acknowledged"])
            with self.assertRaises(Refusal):
                api.completion(self.document, self.bodies.__getitem__)
        api = self.client([[self.hashes[0]], self.hashes[1:]])
        self.session(api)
        self.response(api, {"jwt": FINAL})
        self.assertFalse(self.bucket(api)["acknowledged"])

    def test_handoff_tamper_refuses_before_session_bucket_or_completion(self):
        api = self.client()
        self.session(api)
        self.bodies[self.document["files"]["index.html"]["blob"]] = b"changed"
        with self.assertRaises(Refusal):
            self.bucket(api)
        with self.assertRaises(Refusal):
            self.session(self.client())
        with self.assertRaises(Refusal):
            api.completion(self.document, self.bodies.__getitem__)
        self.assertEqual(api._client.open.call_count, 1)

    def test_stale_disabled_unknown_plan_and_malformed_token_refuse_custody(self):
        for token in (None, "", "with space", "line\nbreak", "非ascii"):
            with self.assertRaises(Refusal):
                upload.Uploader(token, self.plan)
        for field, value in (("phase", "unknown"), ("revision", 99), ("registration", "0" * 64)):
            plan = copy.deepcopy(self.plan)
            plan["current"][field] = value
            with self.assertRaises(Refusal):
                upload.Uploader("prepared", plan)

    def test_invalid_bucket_selector_never_attempts_network(self):
        api = self.client()
        self.session(api)
        for number in (None, True, -1, 1, "0"):
            with self.assertRaises(Refusal):
                self.bucket(api, number)
        self.assertEqual(api._client.open.call_count, 1)

    def test_transformed_redirected_oversized_duplicate_and_error_replies_block(self):
        api = self.client()
        bodies = [b'{"success":true,"success":true,"errors":[],"result":{}}', b"x" * (evidence.LIMIT + 1), b'{"success":true,"result":{}}', b'{"success":true,"errors":[1],"result":{}}']
        for body in bodies:
            api = self.client()
            api._client.open.side_effect = lambda request, timeout: Response(body, request.full_url)
            self.assertFalse(self.session(api)["acknowledged"])
        for attribute, value in (("status", 202), ("url", "https://foreign.example/"), ("headers", {"Content-Encoding": "gzip"})):
            api = self.client()
            reply = Response(b'{}', "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/scripts/crest-development/assets-upload-session")
            setattr(reply, attribute, value)
            api._client.open.return_value, api._client.open.side_effect = reply, None
            self.assertFalse(self.session(api)["acknowledged"])

    def test_context_cleanup_failure_is_unknown_even_after_success_body(self):
        api = self.client([])
        response = mock.MagicMock()
        response.__enter__.return_value = Response(json.dumps({"success": True, "errors": [], "result": {"jwt": JWT, "buckets": []}}).encode(), "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/scripts/crest-development/assets-upload-session")
        response.__exit__.side_effect = OSError("private")
        api._client.open.side_effect, api._client.open.return_value = None, response
        self.assertFalse(self.session(api)["complete"])

    def test_single_asset_mode_refuses_without_fallback(self):
        token = "header." + base64.urlsafe_b64encode(b'{"wrangler_single_asset_uploads":true}').decode().rstrip("=") + ".private"
        api = self.client()
        self.response(api, {"jwt": token, "buckets": [self.hashes]}, 200)
        self.assertFalse(self.session(api)["acknowledged"])
        with self.assertRaises(Refusal):
            self.bucket(api)
        self.assertEqual(api._client.open.call_count, 1)

    def test_proxy_free_redirect_refusal_and_arbitrary_endpoint_refusal(self):
        with mock.patch.object(upload.urllib.request, "build_opener") as opener:
            upload.Uploader("prepared", self.plan)
            handlers = opener.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            with self.assertRaises(Refusal):
                handlers[1].redirect_request(None)
        api = self.client()
        with self.assertRaises(Refusal):
            api._post("/workers/scripts/production", ({}, b""), "prepared")
        api._client.open.assert_not_called()

    def test_detached_plan_and_hash_collision_refuse_before_transfer(self):
        plan = copy.deepcopy(self.plan)
        api = upload.Uploader("prepared", plan)
        plan["registration"]["mapping"]["resource"] = "foreign"
        self.assertEqual(api._selected["parent"], "crest-development")
        with mock.patch.object(payload, "digest", return_value="0" * 32), self.assertRaises(Refusal):
            self.session(self.client())

    def test_concurrent_session_calls_share_only_one_attempt(self):
        api = self.client([])
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.session, api) for _ in range(2)]
            results, refused = [], 0
            for future in futures:
                try:
                    results.append(future.result())
                except Refusal:
                    refused += 1
        self.assertEqual(refused, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["complete"])
        self.assertEqual(api._client.open.call_count, 1)

    def test_cached_completion_revalidates_bytes_before_private_return(self):
        api = self.client([])
        self.session(api)
        self.bodies[self.document["files"]["index.html"]["blob"]] = b"changed"
        with self.assertRaises(Refusal):
            api.completion(self.document, self.bodies.__getitem__)

    def test_admitted_uppercase_extension_preserves_bytes_and_static_type(self):
        files = {"INDEX.HTML": b"<h1>verified</h1>"}
        held = payload.digest("INDEX.HTML", files["INDEX.HTML"])
        headers, body = upload.multipart((held,), files, "a" * 64)
        self.assertIn(b"Content-Type: text/html\r\n", body)
        self.assertIn(base64.b64encode(files["INDEX.HTML"]), body)
        self.assertEqual(headers["Content-Length"], str(len(body)))
