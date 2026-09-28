import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import process
from tests.lib.cargo.test_basis import Repository as Workspace
from tests.lib.media.repository import Repository


def state(root):
    held = {"refs": process.git(root, "for-each-ref", "--format=%(refname) %(objectname)")}
    for name in ("config", "index", "HEAD"):
        held[name] = (root / ".git" / name).read_bytes()
    return held


class Sentinel(unittest.TestCase):
    def setUp(self):
        self.sentinel = Path(tempfile.mkdtemp())
        process.git(self.sentinel, "init", "-q")
        (self.sentinel / "kept").write_text("kept\n")
        process.git(self.sentinel, "add", "kept")
        process.git(self.sentinel, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sentinel")
        process.git(self.sentinel, "tag", "sentinel")
        self.before = state(self.sentinel)
        self.pointed = {
            "GIT_DIR": str(self.sentinel / ".git"),
            "GIT_INDEX_FILE": str(self.sentinel / ".git" / "index"),
            "GIT_WORK_TREE": str(self.sentinel),
        }

    def test_helpers_leave_a_repository_named_by_the_environment_untouched(self):
        with mock.patch.dict(os.environ, self.pointed):
            repository = Repository()
            repository.git("tag", "-a", "v1.0.0", "-m", "v1.0.0")
            workspace = Workspace()
            listed = process.git(repository.root, "ls-files")
        self.assertEqual(state(self.sentinel), self.before)
        self.assertEqual(process.git(self.sentinel, "tag", "-l"), "sentinel")
        self.assertEqual(process.git(repository.root, "tag", "-l"), "v1.0.0")
        self.assertIn("package.json", listed.splitlines())
        self.assertIn("Cargo.toml", process.git(workspace.root, "ls-files").splitlines())

    def test_every_variable_git_calls_local_to_a_repository_is_cleared(self):
        local = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(set(local) - set(process.REPOSITORY), set())
        with mock.patch.dict(os.environ, self.pointed):
            self.assertEqual(set(process.unanchored()) & set(process.REPOSITORY), set())
