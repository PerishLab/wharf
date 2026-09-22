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
        self.repository = Repository({"skills/demo/SKILL.md": "# demo\n", "skills/demo/refs/a.md": "a\n"})
        self.repository.git("tag", "-a", "v1.0.0", "-m", "v1.0.0")

    def copy(self, files):
        root = Path(tempfile.mkdtemp())
        for relative, body in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body)
        return root

    def test_a_skill_that_matches_the_marker_tree_passes(self):
        held = self.copy({"SKILL.md": "# demo\n", "refs/a.md": "a\n"})
        self.assertIn("matches skills/demo", consigned.skill(self.repository.root, "v1.0.0", "demo", held))

    def test_a_skill_that_drifts_from_the_marker_tree_refuses(self):
        held = self.copy({"SKILL.md": "# changed\n", "refs/a.md": "a\n"})
        with self.assertRaisesRegex(Refusal, "differs from skills/demo at v1.0.0: SKILL.md"):
            consigned.skill(self.repository.root, "v1.0.0", "demo", held)

    def test_a_marker_without_the_skill_root_refuses(self):
        with self.assertRaisesRegex(Refusal, "carries no skills/other"):
            consigned.carried(self.repository.root, "v1.0.0", "other")

    def test_changelog_notes_pass_only_when_plumb_proves_them(self):
        with mock.patch.dict(os.environ, fake(0)):
            self.assertEqual(consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp()), "proved")
        with mock.patch.dict(os.environ, fake(1)), self.assertRaisesRegex(Refusal, "refused the consigned notes"):
            consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp())
