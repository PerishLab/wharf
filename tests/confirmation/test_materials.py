import base64
import gzip
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content.static import evidence
from lib.identity.bind.spec import entry
from lib.process.confirmation import materials
from lib.refusal import Refusal


class Materials(unittest.TestCase):
    def git(self, root, *arguments):
        environment = {**evidence.clean(os.environ, Path.home()), "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid", "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}
        return subprocess.check_output(["git", "-C", str(root), *arguments], env=environment, stderr=subprocess.DEVNULL).decode().strip()

    def fixture(self, root):
        self.git(root, "init", "--initial-branch=main")
        (root / "Cargo.toml").write_text('name="fixture"\nversion="0.140"\n')
        (root / ".gitattributes").write_text("hidden.txt export-ignore\n")
        (root / "hidden.txt").write_bytes(b"expressed source must not disappear\n")
        (root / "binary.bin").write_bytes(b"\0\xff\n")
        (root / "run.sh").write_text("fixture source, never executed\n")
        (root / "run.sh").chmod(0o755)
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "base")
        source = {"repository": "PerishLab/plumb", "marker": "v0.141.0-rc.1", "commit": self.git(root, "rev-parse", "HEAD"), "tree": self.git(root, "rev-parse", "HEAD^{tree}")}
        (root / "Cargo.toml").write_text('name="fixture"\nversion="0.141"\n')
        self.git(root, "add", "Cargo.toml")
        self.git(root, "commit", "-m", "effective dependencies")
        world = {"source": source, "head": self.git(root, "rev-parse", "HEAD"), "tree": self.git(root, "rev-parse", "HEAD^{tree}")}
        return source, world

    def test_inherited_git_context_cannot_change_another_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            held = Path(directory)
            sentinel, product = held / "sentinel", held / "product"
            sentinel.mkdir()
            product.mkdir()
            self.fixture(sentinel)
            retained = {name: (sentinel / ".git" / name).read_bytes() for name in ("HEAD", "config", "index", "refs/heads/main")}
            environment = {"GIT_DIR": str(sentinel / ".git"), "GIT_WORK_TREE": str(sentinel), "GIT_COMMON_DIR": str(sentinel / ".git"), "GIT_INDEX_FILE": str(sentinel / ".git/index")}
            with mock.patch.dict(os.environ, environment):
                source, world = self.fixture(product)
                payload = materials.capture(product, source, world)
            self.assertEqual(payload["source"], source)
            self.assertEqual(payload["head"], self.git(product, "rev-parse", "HEAD"))
            self.assertEqual(self.git(product, "rev-parse", "--show-toplevel"), str(product))
            self.assertEqual(self.git(sentinel, "status", "--porcelain"), "")
            self.assertEqual(retained, {name: (sentinel / ".git" / name).read_bytes() for name in retained})

    def test_complete_effective_bytes_ignore_export_omissions_and_preserve_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, world = self.fixture(root)
            payload = materials.capture(root, source, world)
            self.assertEqual(payload["source"], source)
            self.assertNotEqual(payload["source"]["tree"], payload["tree"])
            self.assertEqual(payload["head"], world["head"])
            raw = gzip.decompress(base64.b64decode(payload["payload"]["data"], validate=True))
            self.assertEqual(entry(raw), {key: payload["payload"][key] for key in ("sha256", "size")})
            records = json.loads(raw)
            self.assertEqual(payload["files"], 5)
            self.assertEqual(len(records), 5)
            files = {record["path"]: record for record in records}
            self.assertEqual(base64.b64decode(files["hidden.txt"]["data"]), (root / "hidden.txt").read_bytes())
            self.assertEqual(base64.b64decode(files["binary.bin"]["data"]), b"\0\xff\n")
            self.assertIn(b'"0.141"', base64.b64decode(files["Cargo.toml"]["data"]))
            self.assertEqual(files["Cargo.toml"]["oid"], self.git(root, "hash-object", "Cargo.toml"))
            self.assertEqual(files["run.sh"]["mode"], "100755")

    def test_moved_tree_or_world_identity_cannot_label_different_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, world = self.fixture(root)
            for changed in ({"head": "f" * 40}, {"tree": source["tree"]}, {"source": source | {"commit": "e" * 40}}):
                with self.subTest(changed=changed), self.assertRaises(Refusal):
                    materials.capture(root, source, world | changed)

    def test_links_cannot_be_dropped_from_complete_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, world = self.fixture(root)
            (root / "alias").symlink_to("hidden.txt")
            self.git(root, "add", "alias")
            self.git(root, "commit", "-m", "link")
            world = world | {"head": self.git(root, "rev-parse", "HEAD"), "tree": self.git(root, "rev-parse", "HEAD^{tree}")}
            with self.assertRaisesRegex(Refusal, "regular Git blobs"):
                materials.capture(root, source, world)

    def test_duplicate_paths_oversized_and_changed_blob_bytes_refuse(self):
        row = b"100644 blob " + b"a" * 40 + b"       1\ta\0"
        with self.assertRaises(Refusal):
            materials.listed(row + row)
        with self.assertRaises(Refusal):
            materials.listed(row.replace(b"       1", b" 999999999"))
        for malformed in (b"missing separator\0", b"100644 blob oid\ta\0", b"100644 blob " + b"a" * 40 + b" missing\ta\0"):
            with self.subTest(malformed=malformed), self.assertRaises(Refusal):
                materials.listed(malformed)
        records = [{"path": "a", "mode": "100644", "oid": "a" * 40, "size": 1}]
        with self.assertRaisesRegex(Refusal, "content differs"):
            materials.decoded(records, b"a" * 40 + b" blob 1\nx\n")
