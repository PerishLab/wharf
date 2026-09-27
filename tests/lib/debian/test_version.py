import shutil
import subprocess
import unittest

from lib.content import marker
from lib.debian.version import debian
from lib.refusal import Refusal

MARKERS = ["v1.0.0-alpha.1", "v1.0.0-alpha.2", "v1.0.0-beta.1", "v1.0.0-rc.9", "v1.0.0-rc.10", "v1.0.0", "v1.0.1-alpha.1", "v1.0.1", "v1.10.0"]


class Version(unittest.TestCase):
    def test_a_stable_marker_drops_its_v(self):
        self.assertEqual(debian("v1.2.3"), "1.2.3")

    def test_a_prerelease_sorts_before_its_release_with_a_tilde(self):
        self.assertEqual([debian(held) for held in ("v1.2.3-alpha.1", "v1.2.3-beta.4", "v1.2.3-rc.2")], ["1.2.3~alpha.1", "1.2.3~beta.4", "1.2.3~rc.2"])

    def test_what_is_not_a_marker_refuses(self):
        for held in ("1.2.3", "v1.2.3-rc.0", "v1.2.3-dev.1", "latest"):
            with self.subTest(held), self.assertRaises(Refusal):
                debian(held)

    def test_the_markers_order_is_the_order_the_markers_keep(self):
        self.assertEqual(sorted(MARKERS, key=marker.order), MARKERS)

    @unittest.skipUnless(shutil.which("dpkg"), "dpkg compares Debian versions")
    def test_dpkg_orders_the_versions_as_the_markers_order(self):
        for lower, higher in zip(MARKERS, MARKERS[1:]):
            with self.subTest(lower=lower, higher=higher):
                self.assertEqual(subprocess.run(["dpkg", "--compare-versions", debian(lower), "lt", debian(higher)]).returncode, 0)
