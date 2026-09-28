import tempfile
import unittest
from pathlib import Path

from lib.refusal import Conflict, Refusal
from lib.store.directory import Directory


class Stored(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.bucket = Directory(self.root)

    def test_an_object_is_a_file_under_its_key(self):
        self.bucket.create("v1/objects/a.json", b"{}")
        self.assertTrue(self.bucket.exists("v1/objects/a.json"))
        self.assertEqual((self.root / "v1/objects/a.json").read_bytes(), b"{}")
        self.assertEqual(self.bucket.get("v1/objects/a.json"), b"{}")

    def test_create_refuses_an_existing_key_and_put_replaces_it(self):
        self.bucket.create("a", b"1")
        with self.assertRaises(Conflict):
            self.bucket.create("a", b"2")
        self.bucket.put("a", b"2")
        self.assertEqual(self.bucket.get("a"), b"2")

    def test_a_missing_key_answers_404(self):
        with self.assertRaisesRegex(Refusal, "404"):
            self.bucket.get("missing")

    def test_a_key_cannot_leave_the_directory(self):
        for key in ("", "/etc/passwd", "../outside"):
            with self.assertRaisesRegex(Refusal, "plain relative key"):
                self.bucket.exists(key)
