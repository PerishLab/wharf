import http.client
import unittest
from unittest import mock

from lib.process.lane import publication
from lib.refusal import Conflict, Refusal
from lib.store import r2
from lib.store.sigv4 import Credentials


class Transport(unittest.TestCase):
    def prepared(self, status=200, body=b"{}"):
        response = mock.Mock(status=status)
        response.getheader.return_value = None
        response.read.return_value = body
        response.getheaders.return_value = []
        connection = mock.Mock()
        connection.getresponse.return_value = response
        bucket = publication.Bucket("https://" + "a" * 32 + ".r2.cloudflarestorage.com", "lane-evidence", Credentials("evidence-id", "evidence-secret"))
        self.enterContext(mock.patch.object(bucket, "connect", return_value=connection))
        return bucket, connection, response

    def test_create_only_and_collision_get_are_fixed_signed_and_bounded(self):
        bucket, connection, response = self.prepared()
        key = "lane/v1/evidence/" + "1" * 64 + ".json"
        bucket.create(key, b"{}", {"Content-Type": "application/json"})
        request = connection.request.call_args
        self.assertEqual(request.args, ("PUT", "/lane-evidence/" + key))
        self.assertEqual(request.kwargs["headers"]["if-none-match"], "*")
        self.assertIn("authorization", request.kwargs["headers"])
        response.read.assert_called_once_with(65537)
        connection.close.assert_called_once()
        connection.reset_mock()
        self.assertEqual(bucket.get(key), b"{}")
        self.assertEqual(connection.request.call_args.args, ("GET", "/lane-evidence/" + key))
        connection.close.assert_called_once()

    def test_no_general_write_method_query_or_mutable_operation_is_reachable(self):
        bucket, connection, _ = self.prepared()
        for operation in (r2.Operation("DELETE", "lane/v1/evidence/x"), r2.Operation("PUT", "lane/v1/evidence/x"),
                          r2.Operation("GET", "lane/v1/evidence/x", query={"versionId": "other"})):
            with self.assertRaises(Refusal):
                bucket.request(operation)
        connection.request.assert_not_called()

    def test_oversize_transformed_redirect_or_http_failure_refuses_without_retry(self):
        for status, body, encoding in ((200, b" " * 65537, None), (200, "not-bytes", None), (200, b"{}", "gzip"),
                                       (301, b"{}", None), (302, b"{}", None), (404, b"{}", None), (500, b"{}", None)):
            bucket, connection, response = self.prepared(status, body)
            response.getheader.return_value = encoding
            with self.subTest(status=status, encoding=encoding), self.assertRaises(Refusal):
                bucket.get("lane/v1/evidence/x")
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()

    def test_create_collision_is_distinct_and_does_not_retry(self):
        bucket, connection, _ = self.prepared(412)
        with self.assertRaises(Conflict):
            bucket.create("lane/v1/evidence/x", b"{}")
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_transport_and_cleanup_failures_are_sanitized_and_closed(self):
        for target in ("request", "getresponse", "close"):
            bucket, connection, _ = self.prepared()
            getattr(connection, target).side_effect = OSError("private provider diagnostic")
            with self.subTest(target=target), self.assertRaises(Refusal) as caught:
                bucket.get("lane/v1/evidence/x")
            self.assertNotIn("private provider diagnostic", str(caught.exception))
            connection.close.assert_called_once()
        bucket, connection, _ = self.prepared()
        connection.getresponse.side_effect = http.client.HTTPException("private")
        with self.assertRaises(Refusal):
            bucket.get("lane/v1/evidence/x")
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
