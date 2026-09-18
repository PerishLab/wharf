import json
import tempfile
import unittest
from pathlib import Path

from lib.cargo import build
from lib.refusal import Refusal


def metadata(packages):
    listed = [{"name": name, "targets": [{"name": binary, "kind": ["bin"]} for binary in binaries]} for name, binaries in packages]
    return json.dumps({"packages": listed})


class Cargo:
    def __init__(self, packages, produce=True):
        self.packages = packages
        self.produce = produce
        self.calls = []

    def __call__(self, argv, cwd, env=None):
        self.calls.append((argv, env))
        if argv[:2] == ["cargo", "metadata"]:
            return metadata(self.packages)
        if argv[0] == "rustc":
            return "rustc 1.96.1 (fixture)\n"
        if argv[:2] == ["cargo", "build"] and self.produce:
            built = Path(env["CARGO_TARGET_DIR"]) / argv[argv.index("--target") + 1] / "release" / argv[argv.index("--bin") + 1]
            built.parent.mkdir(parents=True)
            built.write_bytes(b"binary")
        return ""

    def build_env(self):
        return next(env for argv, env in self.calls if argv[:2] == ["cargo", "build"])


class Build(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "Cargo.lock").write_text("")
        self.output = root / "out"

    def build(self, cargo, target="x86_64-unknown-linux-gnu"):
        tools = build.Tools(run=cargo, toolchain=lambda source: {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []})
        return build.build(build.Build(self.source, "plumb", target, self.output), tools)

    def test_builds_one_unbound_binary_with_receipt(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        receipt = self.build(cargo)
        self.assertEqual(receipt["package"], "plumb-cli")
        self.assertEqual(receipt["file"], "plumb-x86_64-unknown-linux-gnu")
        self.assertEqual((self.output / receipt["file"]).read_bytes(), b"binary")
        self.assertEqual(json.loads((self.output / "receipt.json").read_text()), receipt)
        self.assertIn("--locked", next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"]))
        self.assertEqual(cargo.build_env()["RUSTUP_TOOLCHAIN"], "1.96.1")
        self.assertEqual(cargo.build_env()["PLUMB_BUILD_TARGET"], "x86_64-unknown-linux-gnu")
        self.assertEqual(cargo.build_env()["PLUMB_BUILD_CHANNEL"], "unbound")

    def test_refuses_malformed_target_before_side_effects(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        with self.assertRaises(Refusal):
            self.build(cargo, target="linux")
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
