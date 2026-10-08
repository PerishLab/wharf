import contextlib
import io
import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from lib import process
from lib.cargo import build
from tests.lib.cargo.test_basis import Repository
from lib.refusal import Refusal
from tests.lib.identity.test_pe import image


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
            for index in (index for index, part in enumerate(argv) if part == "--bin"):
                target = argv[argv.index("--target") + 1]
                suffix = ".exe" if "windows" in target else ""
                built = Path(env["CARGO_TARGET_DIR"]) / target / "release" / f"{argv[index + 1]}{suffix}"
                built.parent.mkdir(parents=True, exist_ok=True)
                built.write_bytes(f"binary {argv[index + 1]}".encode())
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

    def build(self, cargo, target="x86_64-unknown-linux-gnu", product=("plumb", ("plumb",))):
        tools = build.Tools(run=cargo, stream=cargo, toolchain=lambda: {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []})
        self.reported = io.StringIO()
        with contextlib.redirect_stderr(self.reported):
            return build.build(build.Build(self.source, product[0], product[1], target, self.output), tools)

    def test_what_takes_the_time_is_streamed_and_what_is_read_back_is_captured(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        self.build(cargo)
        streamed = [argv[0] for argv, _ in cargo.calls if argv[:2] in (["cargo", "fetch"], ["cargo", "build"])] + [argv[0] for argv, _ in cargo.calls if argv[0] == "rustup"]
        self.assertEqual(streamed, ["cargo", "cargo", "rustup"])
        self.assertEqual(build.Tools().stream, process.stream)
        self.assertEqual(build.Tools().run, process.run)

    def test_every_phase_reports_what_it_took(self):
        self.build(Cargo([("plumb-cli", ["plumb"])]))
        self.assertEqual([line.split()[0] for line in self.reported.getvalue().splitlines()], ["toolchain", "metadata", "fetch", "build"])

    def test_builds_one_unbound_binary_with_receipt(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        receipt = self.build(cargo)
        self.assertEqual((receipt["packages"], receipt["binaries"]), (["plumb-cli"], ["plumb"]))
        self.assertEqual(list(receipt["files"]), ["plumb-x86_64-unknown-linux-gnu"])
        self.assertEqual((self.output / "plumb-x86_64-unknown-linux-gnu").read_bytes(), b"binary plumb")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["plumb-x86_64-unknown-linux-gnu", "receipt.json"])
        self.assertEqual(json.loads((self.output / "receipt.json").read_text()), receipt)
        self.assertIn("--locked", next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"]))
        self.assertEqual(cargo.build_env()["RUSTUP_TOOLCHAIN"], "1.96.1")
        self.assertEqual(cargo.build_env()["PLUMB_BUILD_TARGET"], "x86_64-unknown-linux-gnu")
        self.assertEqual(cargo.build_env()["PLUMB_BUILD_CHANNEL"], "unbound")

    def test_the_dependencies_are_fetched_before_a_build_that_cannot_reach_the_network(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])
        self.build(cargo)
        spoken = [argv[:2] for argv, _ in cargo.calls]
        self.assertLess(spoken.index(["cargo", "fetch"]), spoken.index(["cargo", "build"]))
        fetch = next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "fetch"])
        self.assertEqual(fetch[argv_target := fetch.index("--target") + 1], "x86_64-unknown-linux-gnu")
        self.assertIn("--offline", next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"]))
        self.assertNotIn("--offline", fetch)

    def test_a_windows_build_compiles_vendored_openssl_with_native_perl(self):
        perl = Path(tempfile.mkdtemp()) / "perl.exe"
        perl.write_text("")
        request = build.Build(Path("."), "demo", ("demo",), "x86_64-pc-windows-msvc", Path("out"))
        with unittest.mock.patch.object(build, "NATIVE_PERL", perl):
            self.assertEqual(build.environment(request, "1.96.1")["OPENSSL_SRC_PERL"], str(perl))
            linux = build.Build(Path("."), "demo", ("demo",), "x86_64-unknown-linux-gnu", Path("out"))
            self.assertNotIn("OPENSSL_SRC_PERL", build.environment(linux, "1.96.1"))

    def test_windows_link_options_preserve_cargo_flags_and_inherited_linker_options(self):
        supplied = {"RUSTFLAGS": "--cfg qualified", "CARGO_ENCODED_RUSTFLAGS": "--cfg\x1fencoded", "LINK": "/OPT:REF", "_LINK_": "/DEBUG /Brepro-", "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS": "--cfg target"}
        windows = build.Build(Path("."), "demo", ("demo",), "x86_64-pc-windows-msvc", Path("out"))
        with unittest.mock.patch.dict(build.os.environ, supplied, clear=True):
            held = build.environment(windows, "1.96.1")
            self.assertEqual(held["_LINK_"], "/DEBUG /Brepro- /Brepro")
            self.assertEqual({key: held[key] for key in supplied if key != "_LINK_"}, {key: value for key, value in supplied.items() if key != "_LINK_"})
            for target in ("x86_64-unknown-linux-gnu", "aarch64-apple-darwin"):
                request = build.Build(Path("."), "demo", ("demo",), target, Path("out"))
                self.assertEqual(build.environment(request, "1.96.1")["_LINK_"], supplied["_LINK_"])

    def test_windows_refuses_missing_reproducible_link_metadata_before_copying(self):
        cargo = Cargo([("plumb-cli", ["plumb"])])

        def compiled(argv, cwd, env=None):
            result = cargo(argv, cwd, env)
            if argv[:2] == ["cargo", "build"]:
                built = Path(env["CARGO_TARGET_DIR"]) / "x86_64-pc-windows-msvc/release/plumb.exe"
                built.write_bytes(image(bytes(64), sections=(".rdata",)))
            return result

        with self.assertRaisesRegex(Refusal, "debug"):
            self.build(compiled, target="x86_64-pc-windows-msvc")
        self.assertFalse((self.output / "plumb-x86_64-pc-windows-msvc.exe").exists())
        self.assertFalse((self.output / "receipt.json").exists())

    def test_one_build_holds_every_executable_under_the_product_identity(self):
        cargo = Cargo([("santi-cli", ["santi"]), ("santi-api", ["santi-api"])])
        receipt = self.build(cargo, product=("santi", ("santi", "santi-api")))
        argv = next(argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"])
        self.assertEqual(argv[argv.index("--release") + 1:argv.index("--target")], ["--package", "santi-cli", "--package", "santi-api", "--bin", "santi", "--bin", "santi-api"])
        self.assertEqual(len([argv for argv, _ in cargo.calls if argv[:2] == ["cargo", "build"]]), 1)
        self.assertEqual((cargo.build_env()["SANTI_BUILD_TARGET"], cargo.build_env()["SANTI_BUILD_CHANNEL"]), ("x86_64-unknown-linux-gnu", "unbound"))
        self.assertNotIn("SANTI_API_BUILD_TARGET", cargo.build_env())
        self.assertEqual(receipt["binaries"], ["santi", "santi-api"])
        self.assertEqual((self.output / "santi-api-x86_64-unknown-linux-gnu").read_bytes(), b"binary santi-api")
        self.assertEqual(sorted(receipt["files"]), ["santi-api-x86_64-unknown-linux-gnu", "santi-x86_64-unknown-linux-gnu"])

    def test_a_hyphenated_product_prefixes_with_underscores(self):
        cargo = Cargo([("demo-tool", ["demo-tool"])])
        self.build(cargo, product=("demo-tool", ("demo-tool",)))
        self.assertEqual(cargo.build_env()["DEMO_TOOL_BUILD_CHANNEL"], "unbound")

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


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.target = build.workspace(self.repository.root)
        self.written = {
            "x86_64-unknown-linux-gnu/release/deps/libitoa-4ca4f21e2a5b3a99.rlib": b"a dependency",
            "x86_64-unknown-linux-gnu/release/.fingerprint/itoa-4ca4f21e2a5b3a99/lib-itoa": b"a fingerprint",
            "x86_64-unknown-linux-gnu/release/deps/libdemo-9b1c3f77aa0e4d21.rlib": b"a member",
            "x86_64-unknown-linux-gnu/release/.fingerprint/demo-cli-77ff11aa22bb33cc/bin-demo": b"a member fingerprint",
            "x86_64-unknown-linux-gnu/release/demo": b"the binary",
            ".rustc_info.json": b"a probe",
        }
        for name, body in self.written.items():
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def packed(self, name="dependencies.tar.gz"):
        return build.archive(self.repository.root, Path(tempfile.mkdtemp()) / name)

    def test_what_the_product_builds_itself_is_left_out(self):
        held = build.held(self.target, {"demo", "demo-cli", "demo_cli"})
        self.assertEqual(held, [
            "x86_64-unknown-linux-gnu/release/.fingerprint/itoa-4ca4f21e2a5b3a99/lib-itoa",
            "x86_64-unknown-linux-gnu/release/deps/libitoa-4ca4f21e2a5b3a99.rlib",
        ])

    def test_two_archives_of_the_same_tree_are_the_same_bytes(self):
        self.assertEqual(self.packed("one.tar.gz").read_bytes(), self.packed("two.tar.gz").read_bytes())

    def test_restoring_brings_back_dependencies_and_nothing_of_the_product(self):
        packed = self.packed()
        shutil.rmtree(self.target)
        build.restore(self.repository.root, packed)
        found = sorted(path.relative_to(self.target).as_posix() for path in self.target.rglob("*") if path.is_file())
        self.assertEqual(found, [
            "x86_64-unknown-linux-gnu/release/.fingerprint/itoa-4ca4f21e2a5b3a99/lib-itoa",
            "x86_64-unknown-linux-gnu/release/deps/libitoa-4ca4f21e2a5b3a99.rlib",
        ])
        self.assertEqual((self.target / "x86_64-unknown-linux-gnu/release/deps/libitoa-4ca4f21e2a5b3a99.rlib").read_bytes(), b"a dependency")

    def test_a_member_artifact_in_the_archive_is_dropped_on_the_way_back(self):
        packed = self.packed()
        build.restore(self.repository.root, packed)
        self.assertFalse((self.target / "x86_64-unknown-linux-gnu/release/demo").exists())
        self.assertFalse((self.target / "x86_64-unknown-linux-gnu/release/deps/libdemo-9b1c3f77aa0e4d21.rlib").exists())

    def test_everything_restored_carries_one_instant_so_cargo_reads_it_as_fresh(self):
        packed = self.packed()
        shutil.rmtree(self.target)
        build.restore(self.repository.root, packed)
        stamps = {path.stat().st_mtime_ns for path in self.target.rglob("*")}
        self.assertEqual(len(stamps), 1)
