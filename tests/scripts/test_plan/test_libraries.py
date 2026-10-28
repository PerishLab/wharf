import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.refusal import Refusal
from lib.process import git
from scripts import plan
from tests.lib.store.memory import Memory


class Libraries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "Cargo.toml").write_text("[workspace]\nmembers=['crates/base']\nresolver='3'\n")
        package = self.root / "crates/base"
        (package / "src").mkdir(parents=True)
        (package / "Cargo.toml").write_text("[package]\nname='base'\nversion='0.0.0'\nedition='2024'\n")
        (package / "src/lib.rs").write_text("pub struct Available;\n")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Wharf", "-c", "user.email=wharf@example.invalid", "commit", "-qm", "library fixture")

    def declare(self, extra=""):
        (self.root / "plumb.toml").write_text(f"[release]\n{extra}[release.cargo]\nregistry='perish'\npackages=['base']\n")

    def entries(self, covered=False):
        def held(source, runner, target):
            return {"entry": {"kind": "cargo-suite", "runner": runner, "target": target}}

        entries = {}
        patches = (
            mock.patch.object(plan.basis, "suite", side_effect=held),
            mock.patch.object(plan.node, "carried", return_value=False),
            mock.patch.object(plan.cfworker, "workers", return_value=[]),
            mock.patch.object(plan.coverage, "tested", return_value=covered),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            plan.suites({"source": str(self.root)}, Memory(), entries)
        return entries

    def test_explicit_library_platforms_plan_native_units_without_binaries(self):
        self.declare("targets=['x86_64-unknown-linux-gnu','aarch64-apple-darwin','x86_64-pc-windows-msvc']\n")
        entries = self.entries()
        self.assertEqual(set(entries), {"suite-linux", "suite-macos", "suite-windows"})
        units = plan.units(entries)["layer-1"]
        actual = {unit["target"]: unit["runner"] for unit in units}
        self.assertEqual(actual, {"x86_64-unknown-linux-gnu": "ubuntu-24.04", "aarch64-apple-darwin": "macos-15", "x86_64-pc-windows-msvc": "windows-2025"})
        self.assertTrue(all(unit["action"] == "suite" for unit in units))

    def test_a_linux_guard_cannot_cover_other_platform_suites(self):
        self.declare("targets=['x86_64-unknown-linux-gnu','aarch64-apple-darwin','x86_64-pc-windows-msvc']\n")
        self.assertEqual(set(self.entries(covered=True)), {"suite-macos", "suite-windows"})

    def test_a_library_can_select_windows_without_implying_linux_or_macos(self):
        self.declare("targets=['x86_64-pc-windows-msvc']\n")
        self.assertEqual(set(self.entries()), {"suite-windows"})

    def test_legacy_libraries_and_binary_products_keep_the_linux_suite(self):
        self.declare()
        self.assertEqual(set(self.entries()), {"suite-linux"})
        self.assertEqual(self.entries(covered=True), {})
        self.declare("binaries=['demo']\ntargets=['x86_64-unknown-linux-gnu','aarch64-apple-darwin']\n")
        self.assertEqual(set(self.entries()), {"suite-linux"})

    def test_empty_and_unknown_explicit_selections_refuse(self):
        for targets in ("[]", "['unknown-target']"):
            self.declare(f"targets={targets}\n")
            with self.subTest(targets=targets), self.assertRaises(Refusal):
                self.entries()
