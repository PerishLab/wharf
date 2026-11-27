import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.cargo import basis, suite
from lib.cargo.suite import check
from lib.process import git
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

LINUX = "x86_64-unknown-linux-gnu"
MACOS = "aarch64-apple-darwin"
WINDOWS = "x86_64-pc-windows-msvc"
ARM64 = "aarch64-unknown-linux-musl"


class Runner:
    def __init__(self, code=0, host=MACOS):
        self.code, self.host = code, host
        self.commands, self.env, self.installed = [], None, []

    def run(self, argv, cwd, env=None):
        if argv[0] == "rustc":
            return f"rustc 1.96.1 (fixture)\nhost: {self.host}\n"
        self.installed.append(argv)
        return ""

    def attempt(self, argv, cwd, env):
        self.commands.append(argv)
        self.env = env
        return self.code, "Finished"

    def tools(self):
        return suite.Tools(
            run=self.run,
            attempt=self.attempt,
            toolchain=lambda: {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []},
        )


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "Cargo.toml").write_text("[workspace]\nmembers=['crates/lib','crates/api']\nresolver='3'\n")
        for name, extra in (("lib", ""), ("api", "\npublish = false\n")):
            crate = self.root / f"crates/{name}"
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text(f"[package]\nname='demo-{name}'\nversion='0.0.0'\nedition='2024'{extra}\n")
            (crate / "src/lib.rs").write_text("pub struct Held;\n")
        (self.root / "Cargo.lock").write_text("version = 4\n\n[[package]]\nname = \"demo-lib\"\nversion = \"0.0.0\"\n\n[[package]]\nname = \"demo-api\"\nversion = \"0.0.0\"\n")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Wharf", "-c", "user.email=wharf@example.invalid", "commit", "-qm", "fixture")

    def declare(self, extra=""):
        (self.root / "plumb.toml").write_text(f"[release]\n[release.cargo]\nregistry='perish'\npackages=['demo-lib']\n{extra}")

    def test_defaults_to_linux_macos_and_windows_and_deduplicates_suites(self):
        self.declare()
        entries = {"suite-linux": {"key": "a" * 64, "decision": "run"}}
        check.plan(self.root, False, Memory(), entries)
        self.assertEqual(set(entries), {"suite-linux", "cargo-check-macos", "cargo-check-windows"})

    def test_skips_linux_when_guard_covered_and_skips_targets_with_native_suites(self):
        self.declare()
        entries = {}
        check.plan(self.root, True, Memory(), entries)
        self.assertEqual(set(entries), {"cargo-check-macos", "cargo-check-windows"})
        full = {"suite-macos": {"key": "a" * 64, "decision": "run"}, "suite-windows": {"key": "b" * 64, "decision": "run"}}
        check.plan(self.root, True, Memory(), full)
        self.assertEqual(set(full), {"suite-macos", "suite-windows"})

    def test_respects_explicit_cargo_targets_and_refuses_invalid_lists(self):
        self.declare(f"targets=['{LINUX}']\n")
        entries = {}
        check.plan(self.root, True, Memory(), entries)
        self.assertEqual(entries, {})
        self.declare(f"targets=['{LINUX}','{ARM64}']\n")
        expanded = {}
        check.plan(self.root, True, Memory(), expanded)
        self.assertEqual(set(expanded), {"cargo-check-arm64"})
        for bad in ("[]", f"['{LINUX}','{LINUX}']", "['unknown-target']", f"['{MACOS}']"):
            self.declare(f"targets={bad}\n")
            with self.subTest(targets=bad), self.assertRaises(Refusal):
                check.targets(self.root)

    def test_basis_hashes_only_publishable_closures_and_invalidates_on_source_change(self):
        first = basis.cargo_check(self.root, ("demo-lib",), MACOS, "macos-15")
        self.assertEqual(list(first["members"]), ["crates/lib"])
        (self.root / "crates/api/src/lib.rs").write_text("pub struct Changed;\n")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Wharf", "-c", "user.email=wharf@example.invalid", "commit", "-qm", "api edit")
        self.assertEqual(basis.cargo_check(self.root, ("demo-lib",), MACOS, "macos-15"), first)
        (self.root / "crates/lib/src/lib.rs").write_text("pub struct Updated;\n")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Wharf", "-c", "user.email=wharf@example.invalid", "commit", "-qm", "lib edit")
        self.assertNotEqual(basis.cargo_check(self.root, ("demo-lib",), MACOS, "macos-15"), first)

    def test_runs_native_online_cargo_check_for_publishable_packages(self):
        runner = Runner(host=MACOS)
        output = self.root / "out"
        receipt = check.check(check.Check(self.root, output, MACOS, ("demo-lib",)), runner.tools())
        self.assertEqual(runner.commands, [["cargo", "check", "--locked", "--target", MACOS, "--lib", "--bins", "-p", "demo-lib"]])
        self.assertNotIn("--offline", runner.commands[0])
        self.assertEqual(receipt["action"], "ship.cargo.check")
        self.assertEqual(receipt["packages"], ["demo-lib"])
        self.assertEqual(json.loads((output / "receipt.json").read_text()), receipt)

    def test_accepts_linux_arm64_host_for_musl_target_and_refuses_foreign_or_failed(self):
        arm = Runner(host="aarch64-unknown-linux-gnu")
        check.check(check.Check(self.root, self.root / "arm-out", ARM64, ("demo-lib",)), arm.tools())
        self.assertEqual(arm.commands[0][4], ARM64)
        foreign = Runner(host=LINUX)
        with self.assertRaisesRegex(Refusal, "needs native host"):
            check.check(check.Check(self.root, self.root / "bad-host", MACOS, ("demo-lib",)), foreign.tools())
        self.assertEqual(foreign.commands, [])
        failing = Runner(code=101, host=WINDOWS)
        with self.assertRaisesRegex(Refusal, "cargo check exited 101"):
            check.check(check.Check(self.root, self.root / "bad-exit", WINDOWS, ("demo-lib",)), failing.tools())
        self.assertFalse((self.root / "bad-exit").exists())
