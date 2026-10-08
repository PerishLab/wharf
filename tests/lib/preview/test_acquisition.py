import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.content.static import workspace
from lib.refusal import Refusal


class Acquisition(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.env = dict(workspace.environment(self.temporary.name), GIT_TEMPLATE_DIR="")
        self.git("init", "--quiet")
        (self.root / "source.txt").write_text("selected source\n")
        (self.root / ".gitignore").write_text("ignored/\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "fixture")
        self.git("remote", "add", "origin", "https://github.com/PerishLab/crest.git")
        self.expected = {"repository": "PerishLab/crest", "commit": self.git("rev-parse", "HEAD"), "tree": self.git("rev-parse", "HEAD^{tree}")}

    def git(self, *arguments):
        result = subprocess.run(["/usr/bin/git", "-C", str(self.root), *arguments], env=self.env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_exact_clean_independent_source(self):
        result = workspace.acquired(self.root, self.expected)
        self.assertEqual(result, {"schema": "wharf.preview.acquisition/v1", "root": str(self.root), **self.expected})
        self.assertFalse((self.root / ".git/hooks").exists())

    def test_nonliteral_foreign_and_dirty_source_refuse(self):
        for changed in ({"commit": "main"}, {"commit": "a" * 40}, {"tree": "b" * 40}, {"repository": "../crest"}, {"repository": "PerishLab/other"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                workspace.acquired(self.root, dict(self.expected, **changed))
        (self.root / "dirty").write_text("untracked")
        with self.assertRaises(Refusal):
            workspace.acquired(self.root, self.expected)

    def test_shallow_shared_hooks_and_ignored_payload_refuse(self):
        for name in ("shallow", "objects/info/alternates", "commondir"):
            path = self.root / ".git" / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text("unsafe\n")
            with self.subTest(name=name), self.assertRaises(Refusal):
                workspace.acquired(self.root, self.expected)
            path.unlink()
        (self.root / ".git/hooks").mkdir()
        with self.assertRaises(Refusal):
            workspace.acquired(self.root, self.expected)
        (self.root / ".git/hooks").rmdir()
        (self.root / "ignored").mkdir()
        (self.root / "ignored/credential").write_text("untrusted")
        with self.assertRaises(Refusal):
            workspace.acquired(self.root, self.expected)

    def test_unsafe_or_credential_configuration_refuses_without_disclosure(self):
        for key in ("http.https://github.com/.extraheader", "credential.helper", "core.hooksPath", "filter.product.smudge", "include.path", "extensions.partialClone"):
            self.git("config", key, "private-fixture-secret")
            with self.subTest(key=key), self.assertRaises(Refusal) as raised:
                workspace.acquired(self.root, self.expected)
            self.assertNotIn("private-fixture-secret", str(raised.exception))
            self.git("config", "--unset", key)

    def test_metadata_symlink_and_linked_checkout_refuse(self):
        (self.root / ".git/linked").symlink_to(self.root / "source.txt")
        with self.assertRaises(Refusal):
            workspace.acquired(self.root, self.expected)
        (self.root / ".git/linked").unlink()
        linked = self.root.parent / "linked"
        self.git("worktree", "add", "--detach", str(linked))
        with self.assertRaises(Refusal):
            workspace.acquired(linked, self.expected)

    def test_readback_does_not_inherit_host_tokens_or_config(self):
        from unittest import mock
        with mock.patch.dict(os.environ, GH_TOKEN="private-host-token", GIT_CONFIG_GLOBAL="/wrong", GIT_CONFIG_COUNT="99"):
            workspace.acquired(self.root, self.expected)

    def test_submodule_entry_refuses_without_recursing(self):
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.expected["commit"] + ",module")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "module")
        expected = dict(self.expected, commit=self.git("rev-parse", "HEAD"), tree=self.git("rev-parse", "HEAD^{tree}"))
        with self.assertRaises(Refusal):
            workspace.acquired(self.root, expected)


if __name__ == "__main__":
    unittest.main()
