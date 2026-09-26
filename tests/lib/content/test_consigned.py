import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import consigned
from lib.refusal import Refusal
from tests.lib.media.repository import Repository


def fake(code):
    folder = Path(tempfile.mkdtemp())
    script = folder / "plumb"
    script.write_text(f"#!/bin/sh\necho proved\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}


class Consigned(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.repository.git("tag", "-a", "v1.0.0", "-m", "v1.0.0")

    def copy(self, files):
        root = Path(tempfile.mkdtemp())
        for relative, body in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body)
        return root

    def test_a_skill_is_vetted_by_its_brief_alone(self):
        held = self.copy({"SKILL.md": "# demo\n", "refs/a.md": "a\n"})
        self.assertIn("SKILL.md of 7 bytes", consigned.skill(held))

    def test_a_skill_without_a_brief_or_over_the_cap_refuses(self):
        with self.assertRaisesRegex(Refusal, "carries no SKILL.md"):
            consigned.skill(self.copy({"PATHS.md": "p\n"}))
        with self.assertRaisesRegex(Refusal, "caps 3072"):
            consigned.skill(self.copy({"SKILL.md": "x" * 3073}))

    def test_changelog_notes_pass_only_when_plumb_proves_them(self):
        with mock.patch.dict(os.environ, fake(0)):
            self.assertEqual(consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp()), "proved")
        with mock.patch.dict(os.environ, fake(1)), self.assertRaisesRegex(Refusal, "refused the consigned notes"):
            consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp())
