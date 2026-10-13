import http.client
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.lane import publication, settlement
from lib.refusal import Conflict, Refusal
from lib.store import r2
from lib.store.sigv4 import Credentials
from scripts import preview
from tests.workflow.settlement import fixtures


class Transport(unittest.TestCase):
    def prepared(self, status=200, body=b"{}"):
        response = mock.Mock(status=status)
        response.getheader.return_value = None
        response.read.return_value = body
        response.getheaders.return_value = [("ETag", '"current"')]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        bucket = settlement.Bucket("https://" + "a" * 32 + ".r2.cloudflarestorage.com", "lane-state", Credentials("state-id", "state-secret"))
        self.enterContext(mock.patch.object(bucket, "connect", return_value=connection))
        return bucket, connection, response

    def test_state_transport_permits_only_signed_create_or_exact_etag_cas(self):
        bucket, connection, response = self.prepared()
        bucket.create("lane/v1/history.json", b"{}")
        self.assertEqual(connection.request.call_args.kwargs["headers"]["if-none-match"], "*")
        bucket.swap("lane/v1/current.json", b"{}", '"original"')
        self.assertEqual(connection.request.call_args.args, ("PUT", "/lane-state/lane/v1/current.json"))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["if-match"], '"original"')
        self.assertEqual(bucket.snapshot("lane/v1/current.json"), (b"{}", '"current"'))
        self.assertTrue(all(call.args == (65537,) for call in response.read.call_args_list))
        self.assertEqual(connection.close.call_count, 3)

    def test_state_transport_rejects_unconditional_write_delete_copy_query_and_empty_cas(self):
        bucket, connection, _ = self.prepared()
        for operation in (r2.Operation("PUT", "lane/v1/current.json"), r2.Operation("DELETE", "lane/v1/current.json"),
                          r2.Operation("HEAD", "lane/v1/current.json"), r2.Operation("GET", "lane/v1/current.json", query={"page": "2"}),
                          r2.Operation("PUT", "lane/v1/current.json", headers={"If-Match": ""})):
            with self.assertRaises(Refusal):
                bucket.request(operation)
        connection.request.assert_not_called()

    def test_conflict_and_unknown_response_never_retry_or_infer_success(self):
        for status in (301, 404, 412, 500):
            bucket, connection, _ = self.prepared(status)
            with self.assertRaises(Conflict if status == 412 else Refusal):
                bucket.swap("lane/v1/current.json", b"{}", '"original"')
            connection.request.assert_called_once()
            connection.close.assert_called_once()
        bucket, connection, _ = self.prepared()
        connection.request.side_effect = http.client.HTTPException("private")
        with self.assertRaises(Refusal) as caught:
            bucket.swap("lane/v1/current.json", b"{}", '"original"')
        self.assertNotIn("private", str(caught.exception))
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_transformed_oversized_nonbytes_and_cleanup_unknown_fail_closed(self):
        for body, encoding in ((b"x" * 65537, None), ("not-bytes", None), (b"{}", "gzip")):
            bucket, connection, response = self.prepared(body=body)
            response.getheader.return_value = encoding
            with self.assertRaises(Refusal):
                bucket.snapshot("lane/v1/current.json")
            connection.close.assert_called_once()
        bucket, connection, _ = self.prepared()
        connection.close.side_effect = OSError("unknown")
        with self.assertRaises(Refusal):
            bucket.swap("lane/v1/current.json", b"{}", '"original"')
        connection.request.assert_called_once()

    def test_state_configuration_has_no_evidence_or_generic_fallback(self):
        env = {"WHARF_LANE_STATE_" + key: value for key, value in {
            "ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "BUCKET": "lane-state",
            "ACCESS_KEY_ID": "state-id", "SECRET_ACCESS_KEY": "state-secret"}.items()}
        configured = publication.configured(env, "STATE")
        self.assertEqual(configured.name, "lane-state")
        for prefix in ("WHARF_LANE_EVIDENCE_", "WHARF_R2_"):
            with self.assertRaises(Refusal):
                publication.configured({name.replace("WHARF_LANE_STATE_", prefix): value for name, value in env.items()}, "STATE")

    def test_exact_writer_cannot_write_other_history_custody_registration_or_body(self):
        held = fixtures.bundle()
        with fixtures.runtime(held):
            before, verified = settlement.prepare(held["held"], held["env"])
            writer = settlement.Writer(before, verified, held["env"])
            calls = (lambda: writer.create(writer.prefix + "/registration.json", b"{}"),
                     lambda: writer.create(writer.history, b"{}"),
                     lambda: writer.swap(writer.prefix + "/current.json", b"{}", '"current"'),
                     lambda: writer.get(writer.prefix + "/custody.json"),
                     lambda: writer.snapshot(writer.history))
            with mock.patch.object(writer, "authorize", side_effect=AssertionError("must refuse before authorization")):
                for invoke in calls:
                    with self.assertRaises(Refusal):
                        invoke()
        self.assertFalse(any(call[0] in ("create", "swap") for call in held["store"].calls))
        for name in ("put", "delete", "copy", "prefixes"):
            self.assertFalse(hasattr(writer, name))

    def test_original_reader_and_state_writer_mapping_mismatch_refuses_before_write(self):
        held = fixtures.bundle()
        held["env"]["WHARF_LANE_READER_BUCKET"] = "other-state"
        with fixtures.runtime(held), self.assertRaises(Refusal):
            settlement.perform(held["held"], held["env"])
        self.assertFalse(any(call[0] in ("create", "swap") for call in held["store"].calls))

    def test_state_job_exposes_only_state_writer_and_consumes_exact_evidence_result(self):
        root = Path(settlement.__file__).resolve().parents[3]
        body = (root / ".github/workflows/lane-evidence.yml").read_text()
        self.assertIn("workflow_call:", body)
        self.assertIn("value: ${{ jobs.state.outputs.settled }}", body)
        body = body.split("\n  state:\n", 1)[1]
        for required in ("needs: [evidence]", "needs.evidence.result == 'success'", "ref: ${{ github.sha }}", "actions: read", "WHARF_LANE_STATE_ACCESS_KEY_ID",
                         "WHARF_REFERENCE: ${{ needs.evidence.outputs.reference }}"):
            self.assertIn(required, body)
        for forbidden in ("workflow_dispatch", "push:", "schedule:", "secrets: inherit", "WHARF_LANE_EVIDENCE_",
                          "WHARF_R2_ACCESS_KEY_ID", "WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_LANE_WORKLOAD_"):
            self.assertNotIn(forbidden, body)
        result = {"phase": "settled", "revision": 2, "reference": {"authority": "wharf", "digest": "1" * 64}}
        with mock.patch.object(settlement, "perform", return_value=result), mock.patch.object(preview.parameters, "answer", side_effect=lambda value: value):
            self.assertEqual(preview.lane_settle({}), {"settled": canonical.encode(result).decode()})
        self.assertFalse((root / ".github/workflows/lane-state.yml").exists())


if __name__ == "__main__":
    unittest.main()
