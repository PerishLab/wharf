import json
import tempfile
import unittest
from pathlib import Path

from wharf.refusal import Refusal
from wharf.store import workload
from wharf.store.r2 import Conflict

KEY = "a" * 64


class Memory:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def exists(self, key):
        return key in self.objects

    def get(self, key):
        return self.objects[key]

    def create(self, key, body):
        if key in self.objects:
            raise Conflict(key)
        self.objects[key] = body
        self.writes.append(key)


class Workload(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "demo").write_bytes(b"binary")
        (self.directory / "receipt.json").write_text("{}")

    def publish(self):
        return workload.publish(self.bucket, KEY, self.directory, {"entry": 1}, {"run": "1"})

    def test_record_is_written_last(self):
        self.assertFalse(workload.reusable(self.bucket, KEY))
        result = self.publish()
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(self.bucket.writes[-1], f"workload/1/{KEY}/record.json")
        self.assertTrue(workload.reusable(self.bucket, KEY))
        record = json.loads(self.bucket.get(f"workload/1/{KEY}/record.json"))
        self.assertEqual(sorted(record["files"]), ["demo", "receipt.json"])

    def test_retry_after_partial_upload_completes(self):
        self.bucket.create(f"workload/1/{KEY}/blobs/demo", b"binary")
        self.assertEqual(self.publish()["state"], "recorded")

    def test_different_bytes_under_the_same_key_refuse(self):
        self.bucket.create(f"workload/1/{KEY}/blobs/demo", b"other")
        with self.assertRaises(Refusal):
            self.publish()

    def test_second_complete_publish_is_already_recorded(self):
        self.publish()
        self.assertEqual(self.publish()["state"], "already-recorded")

    def test_refuses_a_malformed_key(self):
        with self.assertRaises(Refusal):
            workload.reusable(self.bucket, "../x")

    def test_key_ignores_mapping_order(self):
        self.assertEqual(workload.key({"a": 1, "b": 2}, {}), workload.key({"b": 2, "a": 1}, {}))
