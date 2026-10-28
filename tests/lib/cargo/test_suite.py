import contextlib
import ctypes
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.cargo import suite, toolchain
from lib.refusal import Refusal
from lib.process import job, windows

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
    def __init__(self, code=0, log=PASSING, host="x86_64-unknown-linux-gnu"):
        self.code, self.log, self.env, self.host = code, log, None, host

    def run(self, argv, cwd, env=None):
        return f"rustc 1.96.1 (fixture)\nhost: {self.host}\n" if argv[0] == "rustc" else ""

    def attempt(self, argv, cwd, env):
        self.env = env
        return self.code, self.log

    def tools(self):
        return suite.Tools(run=self.run, attempt=self.attempt, toolchain=lambda: {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []})


class Suite(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.request = suite.Suite(root, root / "out", "x86_64-unknown-linux-gnu")
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_records_deterministic_totals_per_target(self):
        runner = Runner()
        receipt = suite.suite(self.request, runner.tools())
        self.assertEqual(receipt["totals"], {"passed": 5, "failed": 0, "ignored": 1})
        self.assertEqual(receipt["target"], "x86_64-unknown-linux-gnu")
        self.assertEqual([item["target"] for item in receipt["targets"]], ["demo src/lib.rs", "cli tests/cli.rs", "demo doctests"])
        self.assertEqual(json.loads((self.request.output / "receipt.json").read_text()), receipt)
        self.assertNotIn("finished", (self.request.output / "receipt.json").read_text())

    def test_isolates_home_and_pins_the_toolchain(self):
        runner = Runner()
        suite.suite(self.request, runner.tools())
        self.assertNotEqual(runner.env["HOME"], str(Path.home()))
        self.assertEqual(runner.env["USERPROFILE"], runner.env["HOME"])
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

    def test_refuses_a_foreign_host_before_running_or_recording_tests(self):
        runner = Runner(host="aarch64-apple-darwin")
        with self.assertRaisesRegex(Refusal, "needs native host"):
            suite.suite(self.request, runner.tools())
        self.assertIsNone(runner.env)
        self.assertFalse(self.request.output.exists())


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
        timeout = 5 if os.name == "nt" else 1
        with contextlib.redirect_stderr(sink), self.assertRaisesRegex(Refusal, f"exceeded {timeout}s"):
            suite.attempt(self.child("import time\nprint('begun',flush=True)\ntime.sleep(30)"), self.root, None, timeout=timeout)
        self.assertEqual(sink.getvalue(), "begun\n")


@unittest.skipUnless(os.name == "nt", "requires native Windows job objects")
class Native(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="wharf-native-suite-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_native_layout_and_an_empty_owned_job(self):
        self.assertEqual(ctypes.sizeof(job.Basic), 64)
        self.assertEqual(ctypes.sizeof(job.Limits), 144)
        self.assertEqual(ctypes.sizeof(job.Accounting), 48)
        with job.Job() as owned:
            self.assertEqual(owned.active(), 0)
        self.assertIsNone(owned.handle)

    def observed(self, body, timeout):
        counts = []

        class Observed(job.Job):
            def active(self):
                count = super().active()
                counts.append(count)
                return count

        sink = io.StringIO()
        with mock.patch.object(windows, "Job", Observed), contextlib.redirect_stderr(sink):
            try:
                result = windows.attempt([sys.executable, "-I", "-c", body], self.root, None, timeout)
            except Refusal:
                self.assertTrue(counts)
                self.assertEqual(counts[-1], 0)
                self.assertIn("grandchild", sink.getvalue())
                raise
        self.assertEqual(counts[-1], 0)
        return result, counts

    def descendant(self):
        marker = self.root / "started"
        child = f"import pathlib,time\npathlib.Path({str(marker)!r}).write_text('ready')\nprint('grandchild',flush=True)\ntime.sleep(60)"
        return marker, child

    def test_timeout_terminates_the_supervisor_command_and_grandchild(self):
        marker, child = self.descendant()
        body = f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-I','-c',{child!r}],stdout=sys.stdout,stderr=sys.stderr)\ntime.sleep(60)"
        with self.assertRaisesRegex(Refusal, "exceeded 5s"):
            self.observed(body, 5)
        self.assertTrue(marker.exists())

    def test_a_parent_exiting_does_not_escape_the_timeout_through_its_pipe(self):
        marker, child = self.descendant()
        body = f"import subprocess,sys\nsubprocess.Popen([sys.executable,'-I','-c',{child!r}],stdout=sys.stdout,stderr=sys.stderr)"
        with self.assertRaisesRegex(Refusal, "exceeded 5s"):
            self.observed(body, 5)
        self.assertTrue(marker.exists())

    def test_normal_completion_also_reaps_a_detached_output_descendant(self):
        marker, child = self.descendant()
        body = f"import pathlib,subprocess,sys,time\nsubprocess.Popen([sys.executable,'-I','-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nfor _ in range(200):\n    if pathlib.Path({str(marker)!r}).exists(): break\n    time.sleep(0.01)\nelse: raise SystemExit(7)\nprint('parent',flush=True)"
        result, counts = self.observed(body, 10)
        self.assertEqual(result, (0, "parent\n"))
        self.assertTrue(marker.exists())
        self.assertGreater(max(counts), 0)
