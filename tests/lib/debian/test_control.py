import unittest

from lib.debian import control
from lib.refusal import Refusal

TEMPLATE = """Package: santi-api
Version: __VERSION__
Section: net
Priority: optional
Maintainer: PerishLab <ops@perish.uk>
Depends: libc6 (>= 2.35), ca-certificates | ca-bundle,
 adduser
Pre-Depends: init-system-helpers:any (>= 1.54~)
Description: santi server
 The service that answers santi.
"""


class Rendered(unittest.TestCase):
    def test_wharf_writes_version_and_architecture_and_keeps_the_rest(self):
        text = control.rendered(TEMPLATE, "santi-api", "0.1.0~rc.2", "amd64")
        lines = text.splitlines()
        self.assertEqual(lines[:3], ["Package: santi-api", "Version: 0.1.0~rc.2", "Architecture: amd64"])
        self.assertIn(" adduser", lines)
        self.assertIn(" The service that answers santi.", lines)
        self.assertTrue(text.endswith("answers santi.\n"))
        self.assertNotIn("__VERSION__", text)

    def test_an_architecture_already_amd64_is_written_once(self):
        text = control.rendered(TEMPLATE.replace("Section: net\n", "Architecture: amd64\nSection: net\n"), "santi-api", "0.1.0", "amd64")
        self.assertEqual(text.count("Architecture:"), 1)

    def test_refusals(self):
        cases = {
            TEMPLATE.replace("Package: santi-api", "Package: santi"): "Package",
            TEMPLATE.replace("Version: __VERSION__", "Version: 1.0.0\nX-Held: __VERSION__"): "leaves Version",
            TEMPLATE + "X-Again: __VERSION__\n": "exactly once",
            TEMPLATE.replace("Section: net", "Architecture: arm64"): "Architecture amd64",
            TEMPLATE.replace("Section: net", "Section: net\nsection: web"): "more than once",
            TEMPLATE.replace("Section: net", "Section: net\n\nSource: santi"): "one paragraph",
            " leading\n" + TEMPLATE: "starts with a field",
            TEMPLATE.replace("Section: net", "not a field"): "is not a field",
            TEMPLATE.replace("adduser", "${misc:Depends}"): "substitution",
        }
        for text, message in cases.items():
            with self.subTest(message), self.assertRaisesRegex(Refusal, message):
                control.rendered(text, "santi-api", "0.1.0", "amd64")


class Depends(unittest.TestCase):
    def test_the_first_alternative_of_every_relation_is_installed(self):
        self.assertEqual(control.depends(TEMPLATE), ["init-system-helpers", "libc6", "ca-certificates", "adduser"])

    def test_no_relations_install_nothing(self):
        self.assertEqual(control.depends("Package: a\nVersion: __VERSION__\n"), [])
