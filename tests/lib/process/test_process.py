import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import process
from lib.refusal import Refusal
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


class Github(unittest.TestCase):
    def test_routes_map_known_endpoints_to_typed_runseal_commands(self):
        sha = "a" * 40
        other = "b" * 40
        cases = [
            ("", ["repository", "PerishLab", "wharf"]),
            ("branches/main", ["branch", "PerishLab", "wharf", "main"]),
            (f"compare/{sha}...{other}", ["lifecycle", "ancestry", "PerishLab", "wharf", sha, other]),
            ("git/ref/tags/v1.0.0", ["ref", "PerishLab", "wharf", "tag", "v1.0.0"]),
            (f"git/tags/{sha}", ["annotation", "PerishLab", "wharf", sha]),
            (f"git/commits/{sha}", ["commit", "PerishLab", "wharf", sha]),
            (f"contents/check/windtun.json?ref={sha}", ["contents", "PerishLab", "wharf", sha, "check/windtun.json"]),
            ("actions/runs/42", ["workflow", "PerishLab", "wharf", "42"]),
            ("actions/runs/42/attempts/2", ["workflow", "PerishLab", "wharf", "42", "--attempt", "2"]),
            ("actions/runs/42/attempts/2/jobs?per_page=100", ["jobs", "PerishLab", "wharf", "42", "--attempt", "2", "--size", "100"]),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(process.route("PerishLab/wharf", path), expected)
                suffix = "/" + path if path else ""
                self.assertEqual(process.endpoint("repos/PerishLab/wharf" + suffix), expected)
        with self.assertRaises(Refusal):
            process.route("PerishLab/wharf", "pulls")
        with self.assertRaises(Refusal):
            process.endpoint("users/octocat")

    def test_github_writes_private_token_file_and_cleans_up(self):
        seen = {}

        def fake(argv, stderr=None, timeout=None):
            token_path = Path(argv[argv.index("--token-file") + 1])
            seen["argv"] = argv
            seen["mode"] = stat.S_IMODE(token_path.stat().st_mode)
            seen["token"] = token_path.read_text(encoding="utf-8")
            seen["path"] = token_path
            seen["stderr"] = stderr
            seen["timeout"] = timeout
            return json.dumps({"version": 1, "github": {"outcome": "observed", "value": {"id": 1}}}).encode()

        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghs_secret"}, clear=True), mock.patch.object(process.subprocess, "check_output", side_effect=fake):
            self.assertEqual(process.github(["repository", "PerishLab", "wharf"], timeout=15), {"id": 1})
        self.assertEqual(seen["argv"][:4], ["runseal", ":", "@github", "--token-file"])
        self.assertEqual(seen["argv"][5:], ["--json", "repository", "PerishLab", "wharf"])
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["token"], "ghs_secret")
        self.assertEqual(seen["stderr"], subprocess.PIPE)
        self.assertEqual(seen["timeout"], 15)
        self.assertFalse(seen["path"].exists())

    def test_github_refuses_missing_token_oversized_or_malformed_envelopes(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(Refusal):
            process.github(["repository", "PerishLab", "wharf"])
        for payload in (b"{}", b'{"version":2,"github":{"outcome":"observed","value":{}}}', b'{"version":1,"github":{"outcome":"refused","value":{}}}'):
            with self.subTest(payload=payload), mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), mock.patch.object(process.subprocess, "check_output", return_value=payload), self.assertRaises(ValueError):
                process.github(["repository", "PerishLab", "wharf"])
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), mock.patch.object(process.subprocess, "check_output", return_value=b"x" * 10), self.assertRaises(Refusal):
            process.github(["repository", "PerishLab", "wharf"], limit=5)
