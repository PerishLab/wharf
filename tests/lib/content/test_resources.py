import unittest

from lib.content import resources
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


TARGETS = {"x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"}


class Targets(unittest.TestCase):
    def test_release_and_build_share_the_target_set_plumb_accepts(self):
        released = set(resources.read_json("releases.json")["targets"])
        built = {target["target"] for target in resources.read_json("build.json")["targets"]}
        message = "the release target set is shared with plumb (PerishLab/plumb#51); change both together"
        self.assertEqual(released, TARGETS, message)
        self.assertEqual(built, TARGETS, message)
