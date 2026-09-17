import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from lib.check import actions, files, imports, source, structure, vocabulary
from lib.process import git

CHECKS = (structure, source, imports, vocabulary, actions)


def findings(root):
    paths = files.listed(root)
    return [finding for check in CHECKS for finding in check.check(root, paths)]


class Repository(unittest.TestCase):
    def test_this_repository_is_clean(self):
        root = git(".", "rev-parse", "--show-toplevel")
        self.assertEqual(findings(root), [])


class Violations(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name, body in {"AGENTS.md": "x\n", "CLAUDE.md": "@AGENTS.md\n", "LICENSE": "x\n"}.items():
            (self.root / name).write_text(body)
        (self.root / "lib").mkdir()

    def found(self, path, body):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body))
        return findings(self.root)

    def test_clean_minimal_repository(self):
        self.assertEqual(self.found("lib/demo.py", "VALUE = 1\n"), [])

    def test_reports_each_violation(self):
        cases = {
            "README.md": "hello\n",
            "notes/plan.py": "VALUE = 1\n",
            "lib/commented.py": "VALUE = 1  # note\n",
            "lib/documented.py": 'def f():\n    """doc"""\n',
            "lib/wide.py": "def f(a, b, c, d, e):\n    return a\n",
            "lib/deep.py": "def f(x):\n    if x:\n        for y in x:\n            while y:\n                with y:\n                    if y:\n                        return y\n",
            "lib/upward.py": "from scripts import selfcheck\n",
            "lib/claimed.py": "wrapper = 1\n",
            "lib/locating.py": "HERE = __file__\n",
        }
        for path, body in cases.items():
            with self.subTest(path):
                self.assertNotEqual(self.found(path, body), [])
                (self.root / path).unlink()

    def test_reports_unpinned_action(self):
        workflow = "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v7\n"
        self.assertNotEqual(self.found(".github/workflows/demo.yml", workflow), [])
