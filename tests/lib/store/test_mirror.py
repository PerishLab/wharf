import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lib.store import mirror
from lib.store.directory import Directory

AUTHORITY = "https://releases.demo.perish.uk"


def fetched(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.headers["Content-Type"], response.read()


class Served(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        bucket = Directory(self.root)
        bucket.put("v1/releases/stable/v1.0.0/seal.json", f'{{"url": "{AUTHORITY}/v1/objects/x.zip"}}'.encode())
        bucket.put("v1/objects/x.zip", AUTHORITY.encode())

    def test_json_names_the_local_authority_and_other_objects_are_served_as_written(self):
        with mirror.served(self.root, AUTHORITY) as url:
            kind, seal = fetched(f"{url}/v1/releases/stable/v1.0.0/seal.json")
            _, body = fetched(f"{url}/v1/objects/x.zip")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertEqual(kind, "application/json")
        self.assertEqual(seal, f'{{"url": "{url}/v1/objects/x.zip"}}'.encode())
        self.assertEqual(body, AUTHORITY.encode())

    def test_a_missing_or_escaping_key_answers_404(self):
        with mirror.served(self.root, AUTHORITY) as url:
            for key in ("missing.json", "../outside"):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    fetched(f"{url}/{key}")
                self.assertEqual(raised.exception.code, 404)
