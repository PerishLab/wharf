import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.identity.bind import Artifact
from lib.refusal import Refusal
from lib.smoke import smoke


def answering(version, failing=None):
    def runner(argv, cwd):
        if argv[1] == failing:
            raise subprocess.CalledProcessError(1, argv, stderr="unbound build")
        return f"{version}\n" if argv[1] == "--version" else "Usage: demo\n"
    return runner


class Smoke(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "demo-x86_64-unknown-linux-gnu").write_bytes(b"binary")
        self.artifact = Artifact(self.directory, "demo", "x86_64-unknown-linux-gnu")
        self.output = self.directory / "out"

    def test_records_surfaces_when_version_matches(self):
        receipt = smoke(self.artifact, self.output, "demo v1.0.0", answering("demo v1.0.0"))
        self.assertEqual(receipt["surfaces"]["--version"], "demo v1.0.0")
        self.assertTrue((self.output / "receipt.json").is_file())

    def test_refuses_unexpected_version(self):
        with self.assertRaises(Refusal):
            smoke(self.artifact, self.output, "demo v1.0.0", answering("demo unbound"))

    def test_refuses_failing_surface(self):
        with self.assertRaises(Refusal):
            smoke(self.artifact, self.output, "demo v1.0.0", answering("demo v1.0.0", failing="--help"))
