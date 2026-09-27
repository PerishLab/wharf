import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.identity.bind import Artifact
from lib.refusal import Refusal
from lib.identity.smoke import configured, smoke


def answering(version, failing=None):
    def runner(argv, cwd):
        if argv[1] == failing:
            raise subprocess.CalledProcessError(1, argv, stderr="unbound build")
        return f"{version}\n" if argv[1] == "--version" else "Usage: demo\n"
    return runner


def named(marker):
    def runner(argv, cwd):
        name = Path(argv[0]).name.rsplit("-x86_64", 1)[0]
        return f"{name} {marker}\n" if argv[1] == "--version" else f"Usage: {name}\n"
    return runner


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "demo-x86_64-unknown-linux-gnu").write_bytes(b"binary")
        self.artifact = Artifact(self.directory, "demo", "x86_64-unknown-linux-gnu")
        self.output = self.directory / "out"

    def test_records_surfaces_when_version_matches(self):
        receipt = smoke([self.artifact], self.output, "v1.0.0", answering("demo v1.0.0"))
        self.assertEqual(receipt["binaries"][0]["surfaces"]["--version"], "demo v1.0.0")
        self.assertTrue((self.output / "receipt.json").is_file())

    def test_every_executable_reports_its_own_name_and_the_marker(self):
        (self.directory / "demo-api-x86_64-unknown-linux-gnu").write_bytes(b"binary")
        server = Artifact(self.directory, "demo-api", "x86_64-unknown-linux-gnu")
        receipt = smoke([self.artifact, server], self.output, "v1.0.0-rc.2", named("v1.0.0-rc.2"))
        self.assertEqual([held["expect"] for held in receipt["binaries"]], ["demo v1.0.0-rc.2", "demo-api v1.0.0-rc.2"])

    def test_an_executable_reporting_the_product_s_name_refuses(self):
        (self.directory / "demo-api-x86_64-unknown-linux-gnu").write_bytes(b"binary")
        server = Artifact(self.directory, "demo-api", "x86_64-unknown-linux-gnu")
        with self.assertRaisesRegex(Refusal, "expected 'demo-api v1.0.0'"):
            smoke([self.artifact, server], self.output, "v1.0.0", answering("demo v1.0.0"))
        self.assertFalse(self.output.exists())

    def test_refuses_unexpected_version(self):
        with self.assertRaises(Refusal):
            smoke([self.artifact], self.output, "v1.0.0", answering("demo unbound"))

    def test_refuses_failing_surface(self):
        with self.assertRaises(Refusal):
            smoke([self.artifact], self.output, "v1.0.0", answering("demo v1.0.0", failing="--help"))

    def test_refuses_nothing_to_smoke(self):
        with self.assertRaisesRegex(Refusal, "at least one"):
            smoke([], self.output, "v1.0.0", answering("demo v1.0.0"))


class Configured(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "plumb-x86_64-unknown-linux-gnu").write_bytes(b"binary")
        self.artifact = Artifact(directory, "plumb", "x86_64-unknown-linux-gnu")

    def test_runs_the_declared_steps_in_a_clean_home(self):
        calls = []

        def runner(argv, cwd, env):
            calls.append((argv[1:], env["HOME"] == cwd))
            return ""

        output = Path(tempfile.mkdtemp()) / "validated"
        receipt = configured(self.artifact, str(output), {"repository": "PerishLab/plumb", "marker": "v0.38.0-rc.1"}, runner)
        self.assertEqual(calls[0][0], ["rule", "list", "--json"])
        self.assertTrue(all(clean for _, clean in calls))
        self.assertEqual(len(receipt["steps"]), len(calls))
        self.assertEqual(json.loads((output / "receipt.json").read_text()), receipt)

    def test_refuses_a_failing_step_and_skips_undeclared_products(self):
        def runner(argv, cwd, env):
            raise subprocess.CalledProcessError(1, argv, stderr="catalogue does not cover every mechanism")

        with self.assertRaisesRegex(Refusal, "clean home"):
            configured(self.artifact, str(Path(tempfile.mkdtemp()) / "refused"), {"repository": "PerishLab/plumb", "marker": "v0.38.0"}, runner)
        declared = configured(self.artifact, str(Path(tempfile.mkdtemp()) / "other"), {"repository": "PerishLab/other", "marker": "v1.0.0"}, runner)
        self.assertEqual(declared["steps"], [])
