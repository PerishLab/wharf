import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.cfworker import apply
from lib.process.lane import entry, execution, reservation, transfer
from lib.refusal import Refusal
from lib.store import handoff, workload
from lib.store.lane import documents
from tests.lib.content.test_assets import FILES
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.store.memory import Memory
from tests.lib.store.test_handoff import receipt
from tests.workflow import fixtures
from tests.workflow.test_execution import prepared


class Transfer(unittest.TestCase):
    def bundle(self):
        recipe = receipt()
        registered = fixtures.registered(recipe["source"]["declaration"])
        intent = fixtures.request(target=registered)
        store = fixtures.Store(registered)
        observed = entry.admit(intent, store, CONTEXT, fixtures.provider())
        directory = self.enterContext(tempfile.TemporaryDirectory())
        destination = Path(directory) / "bundle"
        document = handoff.pack(destination, FILES, recipe)
        bucket = Memory()
        handoff.publish(bucket, destination)
        held = {"request": fixtures.encoded(intent), "artifact": fixtures.encoded(apply.identity(document))}
        with mock.patch.object(entry, "observe", return_value=observed), mock.patch.object(reservation.r2, "configured", return_value=store):
            value = reservation.prepare(held, fixtures.platform())
        held = {"request": held["request"], "prepared": fixtures.encoded(value), "workload": document["key"]}
        return intent, store, bucket, held

    @staticmethod
    def run_transfer(intent, store, bucket, held):
        def observe(request, value, environ):
            return execution.admit(request, value, store, (entry.platform(environ), fixtures.provider()))

        with mock.patch.object(execution, "observe", side_effect=observe), mock.patch.object(transfer, "Reader", return_value=bucket):
            return transfer.prepare(held, fixtures.platform())

    def test_real_recorded_content_and_reservation_roundtrip_without_writer(self):
        intent, store, bucket, held = self.bundle()
        before = copy.deepcopy(bucket.objects)
        store.calls.clear()
        result = self.run_transfer(intent, store, bucket, held)
        self.assertEqual(result["apply"]["workload"], held["workload"])
        self.assertEqual(result["apply"]["artifact"]["build"]["inputs"], held["workload"])
        self.assertEqual(result["apply"]["current"], store.current)
        self.assertEqual(bucket.objects, before)
        self.assertFalse(any(call[0] in ("create", "swap") for call in store.calls))

    def test_missing_tampered_or_foreign_content_never_passes(self):
        for failure in ("missing", "blob", "key", "artifact"):
            intent, store, bucket, held = self.bundle()
            if failure == "missing":
                bucket.objects[workload.prefix(held["workload"]) + "record.json"] = b"{}"
            elif failure == "blob":
                key = next(key for key in bucket.objects if "/blobs/" in key and not key.endswith("bundle.json"))
                bucket.objects[key] = b"different"
            elif failure == "key":
                held["workload"] = "0" * 64
            else:
                value = documents.decode(held["prepared"].encode())
                value["request"]["intent"]["artifact"]["digest"] = "0" * 64
                held["prepared"] = fixtures.encoded(value)
            with self.subTest(failure=failure), self.assertRaises((Refusal, KeyError)):
                self.run_transfer(intent, store, bucket, held)
            self.assertEqual(store.current["phase"], "reserved")

    def test_state_change_during_fetch_blocks_consumer_output(self):
        intent, store, bucket, held = self.bundle()
        original = bucket.get

        def moved(key):
            body = original(key)
            store.target["enabled"] = False
            return body

        with mock.patch.object(bucket, "get", side_effect=moved), self.assertRaises(Refusal):
            self.run_transfer(intent, store, bucket, held)

    def test_inspect_and_dispose_never_fetch_new_product_content(self):
        for operation in ("inspect", "dispose"):
            intent, store, value = prepared(operation)
            held = {"request": fixtures.encoded(intent), "prepared": fixtures.encoded(value), "workload": "none"}
            with mock.patch.object(transfer, "Reader", side_effect=AssertionError("no content reader")):
                self.assertIsNone(self.run_transfer(intent, store, None, held)["apply"])
                with self.assertRaises(Refusal):
                    self.run_transfer(intent, store, None, dict(held, workload="0" * 64))

    def test_outer_writer_is_rejected_before_any_authority_or_content_read(self):
        for name in (*transfer.build.FORBIDDEN, "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN"):
            with mock.patch.object(execution, "observe", side_effect=AssertionError("no read before writer refusal")):
                with self.subTest(name=name), self.assertRaises(Refusal):
                    transfer.prepare({}, {name: "writer"})

    def test_reader_is_fixed_bounded_get_only_without_generic_writer_fallback(self):
        env = {"WHARF_LANE_WORKLOAD_READER_" + key: value for key, value in {
            "ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "BUCKET": "content-workloads",
            "ACCESS_KEY_ID": "reader", "SECRET_ACCESS_KEY": "reader"}.items()}
        reader = transfer.Reader("1" * 64, env)
        self.assertFalse(any(hasattr(reader, name) for name in ("create", "swap", "put", "head")))
        for key in ("lane/v1/current.json", workload.prefix("0" * 64) + "record.json", reader._prefix + "blobs/../basis.json"):
            with self.assertRaises(Refusal):
                reader.get(key)
        with self.assertRaises(Refusal):
            transfer.Reader("1" * 64, {"WHARF_R2_ACCESS_KEY_ID": "generic-writer"})
        response = mock.Mock(status=200)
        response.getheader.return_value = None
        response.read.return_value = b"{}"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(reader, "connect", return_value=connection):
            self.assertEqual(reader.get(reader._prefix + "record.json"), b"{}")
        self.assertEqual(connection.request.call_args.args[0], "GET")
        self.assertEqual(response.read.call_args.args, (1048577,))
        connection.close.assert_called_once()
        for status in (302, 404, 500):
            response.status = status
            with mock.patch.object(reader, "connect", return_value=connection), self.assertRaises(Refusal):
                reader.get(reader._prefix + "record.json")
        response.status = 200
        for header, value in (("Content-Encoding", "gzip"), ("Content-Range", "bytes 0-1/2")):
            response.getheader.side_effect = lambda name: value if name == header else None
            with mock.patch.object(reader, "connect", return_value=connection), self.assertRaises(Refusal):
                reader.get(reader._prefix + "record.json")
        response.getheader.side_effect = None
        response.read.return_value = b"x" * 1048577
        with mock.patch.object(reader, "connect", return_value=connection), self.assertRaises(Refusal):
            reader.get(reader._prefix + "record.json")
        response.read.return_value = b"{}"
        connection.close.side_effect = OSError("lost")
        with mock.patch.object(reader, "connect", return_value=connection), self.assertRaises(Refusal):
            reader.get(reader._prefix + "record.json")

    def test_consumer_job_requires_all_producers_and_no_write_or_product_execution(self):
        root = Path(transfer.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/intake.yml").read_text()
        consumer = workflow.split("\n  consume:\n", 1)[1]
        self.assertIn("needs: [admission, build, reserve]", consumer)
        self.assertIn("needs.reserve.result == 'success'", consumer)
        self.assertIn("needs.build.result == 'success'", consumer)
        self.assertIn("needs.build.result == 'skipped'", consumer)
        self.assertIn("WHARF_LANE_WORKLOAD_READER_SECRET_ACCESS_KEY:", consumer)
        for forbidden in ("WHARF_R2_SECRET_ACCESS_KEY:", "WHARF_LANE_STATE_SECRET_ACCESS_KEY:", "WHARF_LANE_EVIDENCE_", "CLOUDFLARE", "preview-source", "lane-build", "upload-artifact"):
            self.assertNotIn(forbidden, consumer)


if __name__ == "__main__":
    unittest.main()
