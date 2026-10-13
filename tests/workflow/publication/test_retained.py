import unittest
from unittest import mock

from lib.process.lane import publication
from lib.refusal import Refusal
from lib.store.lane import public
from lib.store.sigv4 import Credentials
from tests.workflow.publication import fixtures
from tests.workflow.publication.test_publication import invoke


class Retained(unittest.TestCase):
    def prepared(self):
        fixture = fixtures.bundle()
        reference = invoke(fixture)
        objects = fixture["bucket"].objects
        key = public.location(reference)
        fixture["bucket"] = publication.Bucket("https://" + "a" * 32 + ".r2.cloudflarestorage.com", "lane-evidence", Credentials("id", "secret"))
        fixture["bucket"].objects = objects
        body = b"<Error><Code>ObjectLockedByBucketPolicy</Code></Error>"
        request = self.enterContext(mock.patch.object(fixture["bucket"], "request", side_effect=lambda operation:
            (409, body, {}) if operation.method == "PUT" else (200, objects[key], {})))
        return fixture, reference, request

    def test_retained_identical_collision_requires_private_and_two_public_reads(self):
        fixture, reference, request = self.prepared()
        self.assertEqual(invoke(fixture), reference)
        self.assertEqual([call.args[0].method for call in request.call_args_list], ["PUT", "GET"])
        self.assertEqual(len(fixture["reads"]), 4)
        self.assertFalse(any(call[0] in ("swap", "create") for call in fixture["store"].calls))

    def test_retained_different_or_unavailable_private_bytes_refuse_before_public_read(self):
        for unavailable in (False, True):
            fixture, reference, request = self.prepared()
            fixture["bucket"].objects[public.location(reference)] = b"different"
            if unavailable:
                request.side_effect = [(409, b"<Error><Code>ObjectLockedByBucketPolicy</Code></Error>", {}), (404, b"", {})]
            with self.subTest(unavailable=unavailable), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(len(fixture["reads"]), 2)
            self.assertEqual(request.call_count, 2)

    def test_retained_private_match_never_substitutes_for_public_readback(self):
        fixture, _, request = self.prepared()
        fixture["response"] = fixtures.Response(b"", 404)
        with self.assertRaises(Refusal):
            invoke(fixture)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(fixture["store"].current["phase"], "reserved")

    def test_retained_collision_rechecks_authority_before_and_after_private_read(self):
        for stage in ("PUT", "GET"):
            fixture, _, request = self.prepared()
            original = request.side_effect

            def moved(operation):
                result = original(operation)
                if operation.method == stage:
                    fixture["store"].target["enabled"] = False
                return result

            request.side_effect = moved
            with self.subTest(stage=stage), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(request.call_count, 1 if stage == "PUT" else 2)
            self.assertEqual(len(fixture["reads"]), 2)


if __name__ == "__main__":
    unittest.main()
