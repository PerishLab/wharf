import datetime
import unittest

from lib.store.sigv4 import Credentials, Request, sign

NOW = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.UTC)


class Sign(unittest.TestCase):
    def test_signature_is_deterministic_and_scoped(self):
        request = Request("PUT", "example.r2.cloudflarestorage.com", "/bucket/key", b"body", {"If-None-Match": "*"})
        first = sign(request, Credentials("access", "secret"), NOW)
        self.assertEqual(first, sign(request, Credentials("access", "secret"), NOW))
        self.assertIn("Credential=access/20260917/auto/s3/aws4_request", first["authorization"])
        self.assertIn("if-none-match", first["authorization"])
        self.assertNotEqual(first["authorization"], sign(request, Credentials("access", "other"), NOW)["authorization"])
