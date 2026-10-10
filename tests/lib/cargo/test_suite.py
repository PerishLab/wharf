import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from lib.cargo import suite, toolchain
from lib.refusal import Refusal

PASSING = """
   Compiling demo v0.0.0
     Running unittests src/lib.rs (/tmp/t/debug/deps/demo-0a1b)

running 2 tests
test result: ok. 2 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s

     Running tests/cli.rs (/tmp/t/debug/deps/cli-2c3d)

running 3 tests
test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.20s

   Doc-tests demo

running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


class Sink(io.StringIO):
    def __init__(self, release):
        super().__init__()
        self.release = release

    def write(self, text):
        if text.startswith("first"):
            self.release.touch()
        return super().write(text)


class Runner:
    def __init__(self, code=0, log=PASSING):
        self.code, self.log, self.env = code, log, None

    def run(self, argv, cwd, env=None):
        return "rustc 1.96.1 (fixture)\n" if argv[0] == "rustc" else ""

    def attempt(self, argv, cwd, env):
        self.env = env
        return self.code, self.log

    def tools(self):
        return suite.Tools(run=self.run, attempt=self.attempt, toolchain=lambda: {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []})


class Suite(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.request = suite.Suite(root, root / "out")
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_records_deterministic_totals_per_target(self):
        runner = Runner()
        receipt = suite.suite(self.request, runner.tools())
        self.assertEqual(receipt["totals"], {"passed": 5, "failed": 0, "ignored": 1})
        self.assertEqual([item["target"] for item in receipt["targets"]], ["demo src/lib.rs", "cli tests/cli.rs", "demo doctests"])
        self.assertEqual(json.loads((self.request.output / "receipt.json").read_text()), receipt)
        self.assertNotIn("finished", (self.request.output / "receipt.json").read_text())

    def test_isolates_home_and_pins_the_toolchain(self):
        runner = Runner()
        suite.suite(self.request, runner.tools())
        self.assertNotEqual(runner.env["HOME"], str(Path.home()))
        self.assertEqual(runner.env["RUSTUP_TOOLCHAIN"], "1.96.1")
        self.assertIn("CARGO_HOME", runner.env)

    def test_refuses_failures_and_records_nothing(self):
        failing = PASSING.replace("test result: ok. 3 passed; 0 failed", "test result: FAILED. 2 passed; 1 failed")
        for code, log in ((101, failing), (101, PASSING), (0, "")):
            with self.subTest(code=code), self.assertRaises(Refusal):
                suite.suite(self.request, Runner(code, log).tools())
            self.assertFalse(self.request.output.exists())

    def test_installs_the_declared_components_and_targets(self):
        declared = {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": ["x86_64-unknown-linux-gnu"]}
        self.assertEqual(
            toolchain.install(declared, ["aarch64-apple-darwin"]),
            ["rustup", "toolchain", "install", "1.96.1", "--profile", "minimal", "--component", "clippy", "--component", "rustfmt", "--target", "aarch64-apple-darwin", "--target", "x86_64-unknown-linux-gnu"],
        )


class Attempt(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def child(self, body):
        return [sys.executable, "-I", "-c", body]

    def test_streams_each_line_while_the_suite_runs(self):
        release = self.root / "release"
        body = f"import pathlib,time\nprint('first',flush=True)\nfor _ in range(200):\n    if pathlib.Path({str(release)!r}).exists(): break\n    time.sleep(0.05)\nelse: raise SystemExit(7)\nprint('second')"
        sink = Sink(release)
        with contextlib.redirect_stderr(sink):
            code, log = suite.attempt(self.child(body), self.root, None)
        self.assertEqual((code, log), (0, "first\nsecond\n"))
        self.assertEqual(sink.getvalue(), log)

    def test_returns_a_failing_exit_with_its_output(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, log = suite.attempt(self.child("print('broken');raise SystemExit(101)"), self.root, None)
        self.assertEqual((code, log), (101, "broken\n"))

    def test_refuses_a_suite_that_exceeds_its_timeout(self):
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink), self.assertRaisesRegex(Refusal, "exceeded 1s"):
            suite.attempt(self.child("import time\nprint('begun',flush=True)\ntime.sleep(30)"), self.root, None, timeout=1)
        self.assertEqual(sink.getvalue(), "begun\n")
