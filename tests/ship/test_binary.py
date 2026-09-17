import json
import tempfile
import unittest
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship import binary


def metadata(*packages):
    return json.dumps({
        "packages": [
            {"name": name, "targets": [{"name": bin, "kind": ["bin"]} for bin in bins]}
            for name, bins in packages
        ]
    })


class Cargo:
    """Stands in for rustup, rustc and cargo; cargo build writes the binary it was asked for."""

    def __init__(self, packages, produce=True):
        self.packages = packages
        self.produce = produce
        self.calls = []

    def __call__(self, argv, cwd, env=None):
        self.calls.append((argv, env))
        if argv[:2] == ["cargo", "metadata"]:
            return metadata(*self.packages)
        if argv[0] == "rustc":
            return "rustc 1.96.1 (fixture)\n"
        if argv[:2] == ["cargo", "build"] and self.produce:
            triple = argv[argv.index("--target") + 1]
            name = argv[argv.index("--bin") + 1]
            built = Path(env["CARGO_TARGET_DIR"]) / triple / "release" / name
            built.parent.mkdir(parents=True)
            built.write_bytes(b"binary")
        return ""


class Binary(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "Cargo.lock").write_text("")
        self.output = self.root / "out"

    def build(self, cargo, **overrides):
        args = dict(name="plumb", triple="x86_64-unknown-linux-gnu", toolchain="1.96.1")
        args.update(overrides)
        return binary.build(self.source, args["name"], args["triple"], args["toolchain"], self.output, cargo)

    def test_builds_one_binary_with_receipt(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        receipt = self.build(cargo)
        self.assertEqual(receipt["package"], "plumb-cli")
        self.assertEqual(receipt["file"], "plumb-x86_64-unknown-linux-gnu")
        self.assertEqual(receipt["sha256"], "9a3a45d01531a20e89ac6ae10b0b0beb0492acd7216a368aa062d1a5fecaf9cd")
        self.assertEqual((self.output / receipt["file"]).read_bytes(), b"binary")
        self.assertEqual(json.loads((self.output / "receipt.json").read_text()), receipt)
        build = next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"])
        self.assertIn("--locked", build)
        self.assertTrue(all(env is None or env["RUSTUP_TOOLCHAIN"] == "1.96.1" for _, env in cargo.calls))

    def test_refuses_before_side_effects(self):
        cases = [
            dict(triple="linux"),
            dict(toolchain="stable"),
        ]
        for case in cases:
            cargo = Cargo([("plumb-cli", ["plumb"])])
            with self.subTest(case), self.assertRaises(Refusal):
                self.build(cargo, **case)
            self.assertEqual(cargo.calls, [])

    def test_refuses_unlocked_source(self):
        (self.source / "Cargo.lock").unlink()
        with self.assertRaises(Refusal):
            self.build(Cargo([("plumb-cli", ["plumb"])]))

    def test_refuses_ambiguous_or_missing_binary(self):
        for packages in ([], [("a", ["plumb"]), ("b", ["plumb"])]):
            with self.subTest(packages), self.assertRaises(Refusal):
                self.build(Cargo(packages))

    def test_refuses_existing_output(self):
        self.output.mkdir()
        with self.assertRaises(Refusal):
            self.build(Cargo([("plumb-cli", ["plumb"])]))

    def test_refuses_when_cargo_leaves_nothing(self):
        with self.assertRaises(Refusal):
            self.build(Cargo([("plumb-cli", ["plumb"])], produce=False))
