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


class Cancellation(unittest.TestCase):
    def probe(self, number=None, budget=5):
        with tempfile.TemporaryDirectory(prefix="wharf-command-test-") as directory:
            root = Path(directory)
            pidfile = root / "child.pid"
            child = f"import os,time; from pathlib import Path; Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
            body = f"from lib.process.follow import operation; operation.TIMEOUT={budget!r}; operation.command({[sys.executable, '-c', child]!r}, None, None)"
            process = subprocess.Popen([sys.executable, "-c", body], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            childpid = None
            try:
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                childpid = int(pidfile.read_text())
                if number is not None:
                    process.send_signal(number)
                _, error = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"cancelled" if number is not None else b"finite execution budget", error)
                with self.assertRaises(ProcessLookupError):
                    os.kill(childpid, 0)
            finally:
                if childpid is not None:
                    try:
                        os.killpg(childpid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_sigterm_and_sigint_reap_the_owned_command(self):
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(number=number):
                self.probe(number)

    def test_timeout_reaps_the_owned_command(self):
        self.probe(budget=0.2)

    def test_normal_and_failed_commands_restore_signal_handlers(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        operation.command([sys.executable, "-c", "pass"], None, None)
        with self.assertRaises(Refusal):
            operation.command([sys.executable, "-c", "raise SystemExit(7)"], None, None)
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)
