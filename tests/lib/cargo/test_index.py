import unittest

from lib.cargo import index
from lib.refusal import Refusal
from tests.lib.cargo.test_publish import LOCATION, crate

MANIFEST = """
links = "demo"

[features]
default = ["http"]
http = ["dep:axum"]

[dependencies.axum]
version = "0.8"
optional = true

[dependencies.renamed]
version = "=1.2.3"
package = "demo-macro"
registry-index = "sparse+https://cargo.perish.uk/"

[dependencies.sqlx]
version = ">=0.9, <0.10"
default-features = false
features = ["sqlite"]

[dev-dependencies.tempfile]
version = "3"

[target."cfg(windows)".dependencies.windows]
version = "~0.6"
registry-index = "sparse+https://elsewhere/"
"""


class Entry(unittest.TestCase):
    def setUp(self):
        self.line = index.entry(crate("demo", "1.2.3", MANIFEST), "demo", "1.2.3", LOCATION)
        self.deps = {held["name"]: held for held in self.line["deps"]}

    def test_carries_package_fields(self):
        self.assertEqual((self.line["name"], self.line["vers"], self.line["links"], self.line["yanked"]), ("demo", "1.2.3", "demo", False))
        self.assertEqual(self.line["features"], {"default": ["http"], "http": ["dep:axum"]})
        self.assertEqual(len(self.line["cksum"]), 64)

    def test_requirements_are_rendered_as_cargo_displays_them(self):
        self.assertEqual(self.deps["axum"]["req"], "^0.8")
        self.assertEqual(self.deps["renamed"]["req"], "=1.2.3")
        self.assertEqual(self.deps["sqlx"]["req"], ">=0.9, <0.10")
        self.assertEqual(self.deps["windows"]["req"], "~0.6")

    def test_registry_is_null_only_for_its_own_index(self):
        self.assertIsNone(self.deps["renamed"]["registry"])
        self.assertEqual(self.deps["axum"]["registry"], index.CRATES_IO)
        self.assertEqual(self.deps["windows"]["registry"], "sparse+https://elsewhere/")

    def test_rename_kind_target_and_flags(self):
        self.assertEqual(self.deps["renamed"]["package"], "demo-macro")
        self.assertEqual(self.deps["tempfile"]["kind"], "dev")
        self.assertEqual(self.deps["windows"]["target"], "cfg(windows)")
        self.assertTrue(self.deps["axum"]["optional"])
        self.assertFalse(self.deps["sqlx"]["default_features"])

    def test_refuses_a_crate_naming_another_version(self):
        with self.assertRaises(Refusal):
            index.entry(crate("demo", "1.2.3"), "demo", "1.2.4", LOCATION)
