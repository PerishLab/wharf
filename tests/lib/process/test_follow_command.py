import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lib.process.follow import operation
from lib.refusal import Refusal


SPAWN = """
import os, subprocess
from pathlib import Path
from lib.process.follow import operation
spawn = subprocess.Popen._execute_child
def widened(self, *args):
    if {stage!r} == "before":
        os.kill(os.getpid(), {number})
    spawn(self, *args)
    Path({pidfile!r} + ".held").write_text(str(self.pid))
    os.replace({pidfile!r} + ".held", {pidfile!r})
    if {stage!r} == "starting":
        os.kill(os.getpid(), {number})
subprocess.Popen._execute_child = widened
operation.TIMEOUT = {budget!r}
operation.command({argv!r}, None, None)
"""


class Cancellation(unittest.TestCase):
    def started(self, pidfile):
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return int(pidfile.read_text())

    def probe(self, stage, number=None, budget=5):
        with tempfile.TemporaryDirectory(prefix="wharf-command-test-") as directory:
            pidfile = Path(directory) / "child.pid"
            body = SPAWN.format(stage=stage, number=int(number or 0), pidfile=str(pidfile), budget=budget, argv=[sys.executable, "-c", "import time; time.sleep(60)"])
            process = subprocess.Popen([sys.executable, "-c", body], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if stage == "running":
                    self.started(pidfile)
                    if number is not None:
                        process.send_signal(number)
                _, error = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"cancelled" if number is not None else b"finite execution budget", error)
                if stage != "before":
                    with self.assertRaises(ProcessLookupError):
                        os.kill(self.started(pidfile), 0)
            finally:
                if pidfile.exists():
                    try:
                        os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_cancellation_at_every_stage_reaps_the_owned_command(self):
        for stage in ("before", "starting", "running"):
            for number in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(stage=stage, number=number):
                    self.probe(stage, number)

    def test_timeout_reaps_the_owned_command(self):
        self.probe("running", budget=0.2)

    def test_normal_and_failed_commands_restore_signal_handlers(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        operation.command([sys.executable, "-c", "pass"], None, None)
        with self.assertRaises(Refusal):
            operation.command([sys.executable, "-c", "raise SystemExit(7)"], None, None)
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)
