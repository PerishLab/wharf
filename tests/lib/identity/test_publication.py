import copy
import hashlib
import http.client
import json
import unittest
from unittest.mock import patch

from lib.content import resources
from lib.content.lane import codec
from lib.refusal import Refusal
from lib.store.lane import public, publication
from tests.lib.identity.test_lane import example
from tests.lib.identity.test_operation import example as operation_example
from tests.lib.store.memory import Memory


def record():
    return operation_example("record-deploy")


def declared():
    value = record()["entry"]["value"]
    return {"target": value["request"]["context"]["requested"], "adaptor": "static", "capabilities": value["assessment"]["capabilities"],
            "authorities": {"authorization": "ensign", "publication": "wharf"}}


def reference(body):
    return {"authority": "wharf", "digest": hashlib.sha256(body).hexdigest()}


class Response:
    def __init__(self, body, status=200, encoding=None):
        self.body, self.status, self.encoding = body, status, encoding
        self.limits = []

    def getheader(self, name):
        return self.encoding

    def read(self, limit):
        self.limits.append(limit)
        return self.body


class Connection:
    def __init__(self, response):
        self.response = response
        self.calls, self.closed = [], False

    def request(self, method, path, headers):
        self.calls.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Publication(unittest.TestCase):
    def setUp(self):
        self.body = codec.encode("record", record())
        self.response = Response(self.body)
        self.connection = Connection(self.response)
        self.connect = patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=self.connection)
        self.mock = self.connect.start()
        self.addCleanup(self.connect.stop)
        self.reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        self.writer = Memory()

    def readback(self, body=None):
        body = self.body if body is None else body
        self.response.body = body
        return publication.readback(self.reader, declared(), reference(body), record()["entry"]["value"]["request"]["context"])

    def test_publish_uses_canonical_create_only_bytes_and_independent_get(self):
        held = publication.publish(self.writer, self.reader, declared(), record())
        self.assertEqual(held, reference(self.body))
        key = public.location(held)
        self.assertEqual(self.writer.get(key), self.body)
        self.assertIn("immutable", self.writer.headers[key]["Cache-Control"])
        self.assertEqual(self.mock.call_args.args, ("evidence.example.test",))
        self.assertEqual(self.mock.call_args.kwargs, {"timeout": 30})
        self.assertEqual(self.connection.calls, [("GET", "/" + key, {"Accept": "application/json", "Accept-Encoding": "identity"})])
        self.assertTrue(self.connection.closed)
        self.assertEqual(self.response.limits, [65537])

    def test_existing_identical_bytes_still_require_fresh_public_readback(self):
        first = publication.publish(self.writer, self.reader, declared(), record())
        count = len(self.writer.writes)
        self.assertEqual(publication.publish(self.writer, self.reader, declared(), record()), first)
        self.assertEqual(len(self.writer.writes), count)
        self.assertEqual(len(self.connection.calls), 2)
        self.response.body = b"altered"
        self.assertRaises(Refusal, publication.publish, self.writer, self.reader, declared(), record())

    def test_private_collision_never_uses_a_matching_cached_public_response(self):
        self.writer.put(public.location(reference(self.body)), b"different")
        self.assertRaises(Refusal, publication.publish, self.writer, self.reader, declared(), record())
        self.assertEqual(self.connection.calls, [])
        self.assertEqual(len(self.writer.writes), 1)

    def test_successful_private_write_is_not_successful_publication(self):
        self.response.body = b"missing"
        self.assertRaises(Refusal, publication.publish, self.writer, self.reader, declared(), record())
        self.assertEqual(self.writer.get(public.location(reference(self.body))), self.body)

    def test_unavailable_public_reader_never_falls_back_to_private_storage(self):
        for status in (301, 302, 307, 308, 404, 403, 500):
            self.response.status = status
            self.assertRaises(Refusal, publication.publish, self.writer, self.reader, declared(), record())
        self.assertTrue(self.connection.closed)

    def test_all_record_paths_reject_private_fields_before_any_write(self):
        for field in ("token", "credentials", "payload", "url", "stamp"):
            for path in ((), ("entry",), ("entry", "value"), ("entry", "value", "request"),
                         ("entry", "value", "request", "context"), ("entry", "value", "assessment"),
                         ("entry", "value", "outcome"), ("entry", "value", "outcome", "material", "artifact")):
                value = record()
                node = value
                for part in path:
                    node = node[part]
                node[field] = "private"
                self.assertRaises(Refusal, publication.publish, self.writer, self.reader, declared(), value)
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(self.connection.calls, [])

    def test_wrong_publication_authority_refuses_before_writing_or_reading(self):
        value = declared()
        value["authorities"]["publication"] = "other"
        self.assertRaises(Refusal, publication.publish, self.writer, self.reader, value, record())
        self.assertRaises(Refusal, publication.readback, self.reader, declared(), dict(reference(self.body), authority="other"), record()["entry"]["value"]["request"]["context"])
        self.assertEqual((self.writer.writes, self.connection.calls), ([], []))

    def test_declared_target_adaptor_capabilities_and_authorization_bind(self):
        for field, value in (("target", dict(declared()["target"], lane="preview.other")), ("adaptor", "other"),
                             ("capabilities", ["inspect", "deploy"]),
                             ("authorities", {"authorization": "other", "publication": "wharf"})):
            wrong = declared()
            wrong[field] = value
            self.assertRaises(Refusal, publication.publish, self.writer, self.reader, wrong, record())
        self.assertEqual((self.writer.writes, self.connection.calls), ([], []))

    def test_caller_cannot_supply_an_alternative_reader_or_arbitrary_routing(self):
        self.assertRaises(Refusal, publication.publish, self.writer, object(), declared(), record())
        for origin in ("http://evidence.example.test", "https://user:secret@evidence.example.test", "https://evidence.example.test/", "https://evidence.example.test/path",
                       "https://evidence.example.test?x=1", "https://evidence.example.test#x", "https://evidence.example.test:443", "https://127.0.0.1",
                       "https://localhost", "https://evidence.localhost", "https://evidence.local", "https://EVIDENCE.example.test", "https://[::1]", "https://a..test", "https://a.test\n"):
            self.assertRaises(Refusal, public.prepare, {"authority": "wharf", "origin": origin})
        self.assertRaises(Refusal, public.prepare, {"authority": "wharf", "origin": "https://a.test", "credentials": "secret"})
        self.assertEqual(self.writer.writes, [])

    def test_readback_binds_every_context_field_without_latest_pointer(self):
        for field, value in (("request", "0" * 64), ("revision", 2), ("adaptor", "other"), ("requested", dict(declared()["target"], app="other"))):
            context = copy.deepcopy(record()["entry"]["value"]["request"]["context"])
            context[field] = value
            self.assertRaises(Refusal, publication.readback, self.reader, declared(), reference(self.body), context)
        self.assertTrue(all("latest" not in path and "current" not in path for _, path, _ in self.connection.calls))

    def test_exact_public_applied_operation_can_settle_its_request(self):
        requested = record()["entry"]["value"]["request"]
        self.assertEqual(publication.settle(self.reader, declared(), reference(self.body), requested), record()["entry"]["value"])
        self.assertEqual(self.writer.writes, [])

    def test_same_context_does_not_allow_different_intent_or_precondition(self):
        for path in ("intent", "expected"):
            requested = copy.deepcopy(record()["entry"]["value"]["request"])
            if path == "intent":
                requested[path]["artifact"]["digest"] = "0" * 64
            else:
                requested[path] = {"state": "present", "revision": 1, "digest": "0" * 64}
            self.assertRaises(Refusal, publication.settle, self.reader, declared(), reference(self.body), requested)

    def test_unknown_or_refused_operations_can_publish_but_never_settle(self):
        for outcome, reason in (("unknown", "unavailable"), ("refused", "denied")):
            value = record()
            value["entry"]["value"]["outcome"] = {"outcome": outcome, "reason": reason}
            self.response.body = codec.encode("record", value)
            held = publication.publish(self.writer, self.reader, declared(), value)
            self.assertRaises(Refusal, publication.settle, self.reader, declared(), held, value["entry"]["value"]["request"])

    def test_denied_or_unknown_authorization_can_publish_only_its_finite_result(self):
        for authorization, outcome, reason in (({"state": "unknown"}, "unknown", "unverified"),
                                                ({"state": "denied", "evidence": {"authority": "ensign", "digest": "0" * 64}}, "refused", "denied")):
            value = record()
            value["entry"]["value"]["assessment"]["conditions"]["authorization"] = authorization
            value["entry"]["value"]["outcome"] = {"outcome": outcome, "reason": reason}
            self.response.body = codec.encode("record", value)
            held = publication.publish(self.writer, self.reader, declared(), value)
            self.assertRaises(Refusal, publication.settle, self.reader, declared(), held, value["entry"]["value"]["request"])

    def test_selection_keeps_requested_and_actual_but_is_not_completion(self):
        value = example("record")
        value["entry"]["value"]["context"] = copy.deepcopy(record()["entry"]["value"]["request"]["context"])
        value["entry"]["value"]["observation"]["actual"] = copy.deepcopy(declared()["target"])
        self.response.body = codec.encode("record", value)
        held = publication.publish(self.writer, self.reader, declared(), value)
        observed = publication.readback(self.reader, declared(), held, value["entry"]["value"]["context"])
        self.assertEqual(observed, value)
        self.assertRaises(Refusal, publication.settle, self.reader, declared(), held, record()["entry"]["value"]["request"])

    def test_matching_digest_does_not_admit_noncanonical_or_secret_bytes(self):
        value = record()
        for body in (json.dumps(value).encode(), self.body + b"\n", b'{"schema":2,"schema":2,"entry":{}}', b"\xff"):
            self.assertRaises(Refusal, self.readback, body)
        value["entry"]["value"]["outcome"]["token"] = "private"
        self.assertRaises(Refusal, self.readback, json.dumps(value, separators=(",", ":")).encode())

    def test_changed_digest_never_matches_expected_public_reference(self):
        self.response.body = self.body + b" "
        self.assertRaises(Refusal, publication.readback, self.reader, declared(), reference(self.body), record()["entry"]["value"]["request"]["context"])

    def test_bounded_read_rejects_oversize_text_or_content_transformation(self):
        for body in (b" " * 65537, "not-bytes"):
            self.response.body = body
            self.assertRaises(Refusal, self.reader.read, reference(self.body))
        self.response.encoding = "gzip"
        self.assertRaises(Refusal, self.reader.read, reference(self.body))

    def test_transport_failure_and_cleanup_failure_refuse(self):
        with patch.object(self.connection, "request", side_effect=OSError("private diagnostic")):
            self.assertRaisesRegex(Refusal, "no private fallback", self.reader.read, reference(self.body))
        self.assertTrue(self.connection.closed)
        with patch.object(self.connection, "getresponse", side_effect=http.client.HTTPException("failed")):
            self.assertRaises(Refusal, self.reader.read, reference(self.body))
        with patch.object(self.connection, "close", side_effect=OSError("failed")):
            self.assertRaisesRegex(Refusal, "cleanup failed", self.reader.read, reference(self.body))

    def test_reference_cannot_add_routing_or_private_fields(self):
        for key, value in (("url", "https://other.test"), ("credentials", "private"), ("digest", "../current"), ("authority", "wharf.other")):
            self.assertRaises(Refusal, self.reader.read, dict(reference(self.body), **{key: value}))
        self.assertEqual(self.connection.calls, [])

    def test_writer_error_cannot_be_masked_by_matching_public_bytes(self):
        with patch.object(self.writer, "create", side_effect=OSError("write unknown")):
            self.assertRaises(OSError, publication.publish, self.writer, self.reader, declared(), record())
        self.assertEqual(self.connection.calls, [])

    def test_public_readback_is_detached_and_grants_no_state_mutation(self):
        value = self.readback()
        value["entry"]["value"]["request"]["context"]["revision"] = 7
        self.assertEqual(self.readback(), record())
        self.assertEqual(self.writer.writes, [])

    def test_actual_rust_declared_public_settlement_observations(self):
        cases = resources.read_json("identity/lane-publication-fixtures.json")["cases"]
        self.assertEqual((len(cases), sum(not case["refused"] for case in cases)), (30, 7))
        for case in cases:
            with self.subTest(case=case["id"]):
                reader = public.prepare({"authority": case["reference"]["authority"], "origin": "https://evidence.example.test"})
                self.response.body = case["content"].encode()
                args = (reader, case["declaration"], case["reference"], case["request"])
                if case["refused"]:
                    self.assertRaises(Refusal, publication.settle, *args)
                else:
                    result = publication.settle(*args)
                    self.assertEqual(codec.encode("operation", result), case["encoded"].encode())
