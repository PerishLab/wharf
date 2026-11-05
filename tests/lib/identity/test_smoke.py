import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.content import resources
from lib.identity import format, region
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


BINDING = {
    "product": "demo-tool",
    "marker": "v1.2.3-beta.4",
    "digest": "2" * 64,
    "commit": "3" * 40,
    "workload": "4" * 64,
}
ORIGIN = {"prefix": "DEMO_TOOL", "commit": "1" * 40, "target": "x86_64-unknown-linux-gnu"}


def unbound():
    body = bytearray(format.SIZE)
    for name, value in (("magic", format.MAGIC), ("prefix", b"DEMO_TOOL"), ("commit", b"1" * 40), ("target", b"x86_64-unknown-linux-gnu")):
        start = format.LAYOUT[name][0]
        body[start:start + len(value)] = value
    return bytes(body)


def flipped(body, offset, value=None):
    changed = bytearray(body)
    changed[offset] = changed[offset] ^ 1 if value is None else value
    return bytes(changed)


def cases():
    empty = unbound()
    bound = region.encode(empty, BINDING)
    legacy = bytearray(empty)
    legacy[:16] = b"PLUMB.IDENTITY.1"
    return {
        "unbound.region": (empty, {"origin": ORIGIN, "binding": None}),
        "bound.region": (bound, {"origin": ORIGIN, "binding": BINDING}),
        "refused-checksum.region": (flipped(bound, 256), {"refused": True}),
        "refused-payload.region": (flipped(bound, format.PAYLOAD + 3), {"refused": True}),
        "refused-trailing-payload.region": (flipped(bound, format.SIZE - 1, 1), {"refused": True}),
        "refused-unbound-data.region": (flipped(empty, 300, 1), {"refused": True}),
        "refused-legacy-magic.region": (bytes(legacy), {"refused": True}),
        "refused-noncanonical-padding.region": (flipped(empty, 70, ord("X")), {"refused": True}),
    }


class Fixtures(unittest.TestCase):
    def test_fixtures_match_the_writer(self):
        manifest = resources.read_json("identity/fixtures/manifest.json")
        expected = cases()
        self.assertEqual(sorted(manifest), sorted(expected))
        for name, (body, expect) in expected.items():
            with self.subTest(name):
                self.assertEqual(resources.read_bytes(f"identity/fixtures/{name}"), body)
                self.assertEqual(manifest[name], expect)

    def test_reader_outcomes(self):
        for name, expect in resources.read_json("identity/fixtures/manifest.json").items():
            body = resources.read_bytes(f"identity/fixtures/{name}")
            with self.subTest(name):
                if expect.get("refused"):
                    self.assertRaises(Refusal, region.decode, body)
                else:
                    origin, binding = region.decode(body)
                    self.assertEqual({"origin": origin, "binding": binding}, expect)
