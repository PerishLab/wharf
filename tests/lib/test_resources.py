import unittest

from lib import resources
from lib.refusal import Refusal


class Resources(unittest.TestCase):
    def test_reads_by_name(self):
        self.assertEqual(resources.read_json("identity/format.json")["magic"], "RELEASE.IDENT.V2")
        self.assertIn("identity/fixtures/manifest.json", resources.names("identity/fixtures"))

    def test_refuses_escaping_or_absolute_names(self):
        for name in ("../AGENTS.md", "/etc/passwd", "identity/../../AGENTS.md", "identity\\format.json", ""):
            with self.subTest(name), self.assertRaises(Refusal):
                resources.read_bytes(name)

    def test_refuses_missing_resources(self):
        with self.assertRaises(Refusal):
            resources.read_bytes("identity/missing.json")
