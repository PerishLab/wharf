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

    def test_a_skill_is_exactly_one_regular_brief(self):
        held = self.copy({"SKILL.md": "# demo\n"})
        self.assertIn("SKILL.md of 7 bytes", consigned.skill(held))

    def test_a_skill_without_a_brief_with_companions_or_over_the_cap_refuses(self):
        with self.assertRaisesRegex(Refusal, "carries no SKILL.md"):
            consigned.skill(self.copy({"PATHS.md": "p\n"}))
        with self.assertRaisesRegex(Refusal, "entries beside SKILL.md"):
            consigned.skill(self.copy({"SKILL.md": "# demo\n", "refs/a.md": "a\n"}))
        with self.assertRaisesRegex(Refusal, "caps 3072"):
            consigned.skill(self.copy({"SKILL.md": "x" * 3073}))

    def test_a_skill_refuses_a_linked_brief(self):
        held = self.copy({})
        descriptor, name = tempfile.mkstemp()
        os.close(descriptor)
        target = Path(name)
        target.write_text("# demo\n")
        (held / "SKILL.md").symlink_to(target)
        self.addCleanup(target.unlink)
        with self.assertRaisesRegex(Refusal, "not a regular file"):
            consigned.skill(held)

    def test_changelog_notes_pass_only_when_plumb_proves_them(self):
        with mock.patch.dict(os.environ, fake(0)):
            self.assertEqual(consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp()), "proved")
        with mock.patch.dict(os.environ, fake(1)), self.assertRaisesRegex(Refusal, "refused the consigned notes"):
            consigned.changelog(self.repository.root, "v1.0.0", tempfile.mkdtemp())

    def bundle(self, files):
        root = Path(tempfile.mkdtemp())
        for relative, body in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return root

    def test_configuration_accepts_valid_toml_bundle(self):
        held = self.bundle({"config.toml": b'[service]\nname = "demo"\n', "rules/policy.toml": b"enabled = true\n", "README.txt": b"notes\n"})
        self.assertEqual(consigned.configuration(held), "configuration carries 3 regular files")

    def test_configuration_refuses_invalid_or_empty_toml(self):
        cases = (
            ("not valid TOML", {"bad.toml": b"broken = \n"}),
            ("is empty", {"empty.toml": b""}),
            ("is empty", {"blank.toml": b"   \n\t\n"}),
            ("holds no TOML entries", {"comment.toml": b"# only comment\n"}),
            ("not valid TOML", {"binary.toml": b"\xff\xfe"}),
        )
        for pattern, files in cases:
            with self.subTest(files=files), self.assertRaisesRegex(Refusal, pattern):
                consigned.configuration(self.bundle(files))

    def test_configuration_refuses_empty_directory(self):
        empty = self.bundle({})
        with self.assertRaisesRegex(Refusal, "holds no files"):
            consigned.configuration(empty)
        (empty / "nested").mkdir()
        with self.assertRaisesRegex(Refusal, "holds no files"):
            consigned.configuration(empty)

    def test_configuration_refuses_symlinks(self):
        target_dir = self.bundle({"real.toml": b"enabled = true\n"})
        file_link_root = self.bundle({})
        (file_link_root / "config.toml").symlink_to(target_dir / "real.toml")
        with self.assertRaisesRegex(Refusal, "special object"):
            consigned.configuration(file_link_root)
        dir_link_root = self.bundle({"valid.toml": b"enabled = true\n"})
        (dir_link_root / "linked").symlink_to(target_dir, target_is_directory=True)
        with self.assertRaisesRegex(Refusal, "special object"):
            consigned.configuration(dir_link_root)
        root_link = Path(tempfile.mkdtemp()) / "root"
        root_link.symlink_to(target_dir, target_is_directory=True)
        with self.assertRaisesRegex(Refusal, "not a plain directory"):
            consigned.configuration(root_link)
