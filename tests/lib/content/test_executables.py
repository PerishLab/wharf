import tempfile
import unittest
from pathlib import Path

from lib.content import executables
from lib.refusal import Refusal

LINUX = "x86_64-unknown-linux-gnu"
DARWIN = "aarch64-apple-darwin"
UNION = [LINUX, DARWIN]


def source(body):
    root = Path(tempfile.mkdtemp())
    (root / "plumb.toml").write_text(body)
    return root


class Declared(unittest.TestCase):
    def test_a_single_executable_takes_every_target_and_installs(self):
        held = executables.declared(source('[release]\nbinaries = ["plumb"]\n'), UNION)
        self.assertEqual(held, [executables.Executable("plumb", (LINUX, DARWIN), True)])
        self.assertEqual(executables.primary(held, "plumb"), "plumb")

    def test_a_binary_table_narrows_targets_and_installation(self):
        body = f'[release]\nbinaries = ["santi", "santi-api"]\n[release.binary.santi-api]\ntargets = ["{LINUX}"]\ninstall = false\n'
        held = executables.declared(source(body), UNION)
        self.assertEqual((executables.built(held, LINUX), executables.built(held, DARWIN)), (["santi", "santi-api"], ["santi"]))
        self.assertEqual((executables.installed(held, LINUX), executables.installed(held, DARWIN)), (["santi"], ["santi"]))

    def test_the_primary_is_named_like_the_product_else_the_first(self):
        held = executables.declared(source('[release]\nbinaries = ["santi-api", "santi"]\n'), UNION)
        self.assertEqual(executables.primary(held, "santi"), "santi")
        self.assertEqual(executables.primary(held, "ensign"), "santi-api")
        with self.assertRaisesRegex(Refusal, "declares no executable"):
            executables.primary([], "santi")

    def test_a_placement_names_its_executable_or_takes_the_primary(self):
        body = '[release]\nbinaries = ["ensign", "ensign-api"]\n[release.oci]\nbinary = "ensign-api"\n[release.deb]\nroot = "packaging/deb"\n'
        held = executables.declared(source(body), UNION)
        self.assertEqual(executables.placed(source(body), "oci", held, "ensign"), "ensign-api")
        self.assertEqual(executables.placed(source(body), "deb", held, "ensign"), "ensign")
        self.assertIsNone(executables.placed(source('[release]\nbinaries = ["ensign"]\n'), "deb", held, "ensign"))

    def test_malformed_declarations_refuse(self):
        cases = {
            '[release]\nbinaries = ["a", "a"]\n': "twice",
            '[release]\nbinaries = "a"\n': "must list",
            '[release]\nbinaries = ["a"]\n[release.binary.b]\ninstall = false\n': "does not list",
            '[release]\nbinaries = ["a"]\n[release.binary.a]\nfeatures = []\n': "takes only",
            '[release]\nbinaries = ["a"]\n[release.binary.a]\ntargets = []\n': "at least one target",
            '[release]\nbinaries = ["a"]\n[release.binary.a]\ntargets = ["x86_64-pc-windows-msvc"]\n': "does not",
            '[release]\nbinaries = ["a"]\n[release.binary.a]\ninstall = "no"\n': "true or false",
        }
        for body, message in cases.items():
            with self.subTest(body), self.assertRaisesRegex(Refusal, message):
                executables.declared(source(body), UNION)

    def test_a_placement_naming_an_undeclared_executable_refuses(self):
        body = '[release]\nbinaries = ["ensign"]\n[release.oci]\nbinary = "ensign-api"\n'
        with self.assertRaisesRegex(Refusal, "ensign-api"):
            executables.placed(source(body), "oci", executables.declared(source(body), UNION), "ensign")
