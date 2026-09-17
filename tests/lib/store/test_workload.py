import json
import tempfile
import unittest
from pathlib import Path

from lib.refusal import Refusal
from lib.store import workload
from tests.lib.store.memory import Memory

KEY = "a" * 64


def produced(files):
    directory = Path(tempfile.mkdtemp())
    for name, body in files.items():
        (directory / name).write_bytes(body)
    return workload.Produced(directory, {"entry": 1}, {"run": "1"})


class Publish(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.produced = produced({"demo": b"binary", "receipt.json": b"{}"})

    def test_record_is_written_last(self):
        self.assertFalse(workload.reusable(self.bucket, KEY))
        self.assertEqual(workload.publish(self.bucket, KEY, self.produced)["state"], "recorded")
        self.assertEqual(self.bucket.writes[-1], f"workload/1/{KEY}/record.json")
        self.assertIn(f"workload/1/{KEY}/basis.json", self.bucket.objects)
        self.assertTrue(workload.reusable(self.bucket, KEY))
        record = json.loads(self.bucket.get(f"workload/1/{KEY}/record.json"))
        self.assertEqual(sorted(record["files"]), ["demo", "receipt.json"])

    def test_retry_after_partial_upload_completes(self):
        self.bucket.create(f"workload/1/{KEY}/blobs/demo", b"binary")
        self.assertEqual(workload.publish(self.bucket, KEY, self.produced)["state"], "recorded")

    def test_different_bytes_under_the_same_key_refuse(self):
        self.bucket.create(f"workload/1/{KEY}/blobs/demo", b"other")
        with self.assertRaises(Refusal):
            workload.publish(self.bucket, KEY, self.produced)

    def test_second_complete_publish_is_already_recorded(self):
        workload.publish(self.bucket, KEY, self.produced)
        self.assertEqual(workload.publish(self.bucket, KEY, self.produced)["state"], "already-recorded")

    def test_refuses_a_malformed_key(self):
        with self.assertRaises(Refusal):
            workload.reusable(self.bucket, "../x")

    def test_key_ignores_mapping_order(self):
        self.assertEqual(workload.key({"a": 1, "b": 2}, {}), workload.key({"b": 2, "a": 1}, {}))


class Fetch(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        workload.publish(self.bucket, KEY, produced({"demo": b"binary"}))
        self.destination = Path(tempfile.mkdtemp()) / "out"

    def test_fetches_recorded_files(self):
        workload.fetch(self.bucket, KEY, self.destination)
        self.assertEqual((self.destination / "demo").read_bytes(), b"binary")

    def test_refuses_tampered_blob(self):
        self.bucket.objects[f"workload/1/{KEY}/blobs/demo"] = b"tampered"
        with self.assertRaises(Refusal):
            workload.fetch(self.bucket, KEY, self.destination)

    def test_refuses_unrecorded_workload(self):
        with self.assertRaises(Refusal):
            workload.fetch(self.bucket, "b" * 64, self.destination)
