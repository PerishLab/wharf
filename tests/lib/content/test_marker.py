import unittest

from lib.content import marker
from lib.refusal import Refusal

MALFORMED = ("0.38.3", "v0.38", "v0.38.3-rc", "v0.38.3-rc.0", "v0.38.3-dev.1", "v0.38.3 ", "xv0.38.3")


class Shape(unittest.TestCase):
    def test_a_stable_marker_and_every_stage_are_release_markers(self):
        for value in ("v0.38.3", "v0.38.3-alpha.1", "v0.38.3-beta.18", "v0.38.3-rc.7", "v10.0.0"):
            self.assertTrue(marker.holds(value), value)

    def test_nothing_else_is(self):
        for value in MALFORMED:
            self.assertFalse(marker.holds(value), value)
            with self.assertRaises(Refusal):
                marker.channel(value)

    def test_a_marker_names_its_channel(self):
        self.assertEqual(marker.channel("v0.38.3"), "stable")
        self.assertEqual(marker.channel("v0.38.3-rc.7"), "rc")

    def test_a_stable_release_outranks_every_prerelease_of_the_same_version(self):
        self.assertGreater(marker.order("v0.38.3"), marker.order("v0.38.3-rc.7"))
        self.assertGreater(marker.order("v0.38.3-rc.1"), marker.order("v0.38.3-beta.18"))
        self.assertGreater(marker.order("v0.38.4-alpha.1"), marker.order("v0.38.3"))

    def test_a_version_is_the_marker_without_its_v(self):
        self.assertEqual(marker.version("v0.38.3-rc.7"), "0.38.3-rc.7")


if __name__ == "__main__":
    unittest.main()
