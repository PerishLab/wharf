import json
import unittest

from lib.cargo import registry
from lib.refusal import Refusal


class Registry(unittest.TestCase):
    def test_sparse_index_entries(self):
        self.assertEqual(registry.entry("a"), "1/a")
        self.assertEqual(registry.entry("ab"), "2/ab")
        self.assertEqual(registry.entry("abc"), "3/a/abc")
        self.assertEqual(registry.entry("Plumb-Macro"), "pl/um/plumb-macro")

    def test_published_reads_versions(self):
        lines = "\n".join(json.dumps({"name": "plumb", "vers": vers}) for vers in ("0.1.0", "0.38.0-beta.4"))
        reader = lambda url: lines if url.endswith("pl/um/plumb") else ""
        self.assertTrue(registry.published("https://registry/", "plumb", "0.38.0-beta.4", reader))
        self.assertFalse(registry.published("https://registry/", "plumb", "0.38.0-beta.5", reader))
        self.assertFalse(registry.published("https://registry/", "other", "0.1.0", reader))

    def test_download_follows_the_served_template(self):
        config = json.dumps({"dl": "https://registry/crates/{crate}/{version}/{sha256-checksum}.crate"})
        reader = lambda url: config if url.endswith("config.json") else ""
        self.assertEqual(registry.download("https://registry/", {"name": "plumb", "vers": "1.0.0", "cksum": "ab"}, reader), "crates/plumb/1.0.0/ab.crate")

    def test_download_refuses_a_template_elsewhere(self):
        config = json.dumps({"dl": "https://other/{crate}"})
        with self.assertRaises(Refusal):
            registry.download("https://registry/", {"name": "plumb", "vers": "1.0.0", "cksum": "ab"}, lambda url: config)

