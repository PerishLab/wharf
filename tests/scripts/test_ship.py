import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from scripts import ship


class Ship(unittest.TestCase):
    def test_refusal_exits_two(self):
        argv = ["smoke", "--dir", "/nonexistent", "--name", "demo", "--target", "x86_64-unknown-linux-gnu", "--output", "/nonexistent/out", "--expect", "demo v1.0.0"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            code = ship.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("refused", error.getvalue())

    def test_smoke_key_is_stable(self):
        output = io.StringIO()
        with redirect_stdout(output):
            ship.main(["key-smoke", "--binary-key", "a" * 64, "--basis", "/dev/null"])
            ship.main(["key-smoke", "--binary-key", "a" * 64, "--basis", "/dev/null"])
        keys = [line for line in output.getvalue().splitlines() if '"key"' in line]
        self.assertEqual(len(set(keys)), 1)
