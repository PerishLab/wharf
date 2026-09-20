import unittest

from lib.content import implementation


class Implementation(unittest.TestCase):
    def test_digest_follows_lib_imports(self):
        held = implementation.digest("lib.cargo.build")
        self.assertIn("lib.cargo.build", held)
        self.assertIn("lib.cargo.toolchain", held)
        self.assertIn("lib.process", held)
        self.assertNotIn("lib.identity.smoke", held)

    def test_resourced_adds_resource_content(self):
        held = implementation.resourced(["lib.identity.bind"], ["identity/format.json"])
        self.assertIn("resource:identity/format.json", held)
        self.assertIn("lib.identity.region", held)
