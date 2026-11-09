import base64
import copy
import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.guard import material
from lib.package.source import reconstruct
from lib.process.confirmation import materials
from lib.refusal import Refusal
from lib.store import plan
from tests.confirmation import test_materials


class Reconstruction(unittest.TestCase):
    def fixture(self, root):
        self.root = root
        self.tools = test_materials.Materials()
        self.source, self.world = self.tools.fixture(root)
        self.world.update(schema="wharf.release.snapshot/v1", packages=[], controls={}, domain={}, guard={})
        self.oid = self.tools.git(root, "rev-parse", self.source["commit"] + ":Cargo.toml")
        self.rows = [{"path": "Cargo.toml", "oid": self.oid, "edits": [{"before": 'version="0.140"', "after": 'version="0.141"'}]}]

    def archive(self, root, source):
        self.assertEqual(source, self.source)
        subprocess.run(["git", "clone", "--bare", "--no-hardlinks", "--local", str(self.root), root], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        materials.git(root, "update-ref", "FETCH_HEAD", source["commit"])
        materials.git(root, "read-tree", source["tree"])

    def test_recovered_complete_tree_preserves_hidden_binary_and_executable_blobs(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(Path(directory))
            with mock.patch.object(reconstruct, "archived", side_effect=self.archive):
                payload = reconstruct.recover(self.world, self.rows)
            self.assertEqual(payload["tree"], self.world["tree"])
            self.assertEqual(payload["source"], self.source)
            files = json.loads(gzip.decompress(base64.b64decode(payload["payload"]["data"])))
            self.assertEqual(len(files), 5)
            held = {row["path"]: row for row in files}
            self.assertEqual(base64.b64decode(held["binary.bin"]["data"]), b"\0\xff\n")
            self.assertIn("hidden.txt", held)
            self.assertEqual(held["run.sh"]["mode"], "100755")

    def test_missing_or_wrong_recovery_cannot_stand_in_for_effective_material(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(Path(directory))
            for rows in ([], [self.rows[0] | {"edits": [{"before": 'version="0.140"', "after": 'version="wrong"'}]}]):
                with self.subTest(rows=rows), mock.patch.object(reconstruct, "archived", side_effect=self.archive), self.assertRaisesRegex(Refusal, "complete recovered Git tree"):
                    reconstruct.recover(self.world, rows)

    def test_wrong_archived_blob_and_ambiguous_edits_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(Path(directory))
            for row in (self.rows[0] | {"oid": "f" * 40}, self.rows[0] | {"edits": [{"before": "missing", "after": "new"}]}):
                with self.subTest(row=row), mock.patch.object(reconstruct, "archived", side_effect=self.archive), self.assertRaises(Refusal):
                    reconstruct.recover(self.world, [row])

    def test_recorded_material_is_bound_and_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(Path(directory))
            recorded = {"schema": material.SCHEMA, "snapshot": canonical.digest(plan.combination(self.world)), "files": [{"path": "Cargo.toml", "mode": "100644", "content": (self.root / "Cargo.toml").read_text()}]}
            with mock.patch.object(reconstruct, "archived", side_effect=self.archive):
                self.assertEqual(reconstruct.recover(self.world, [], recorded)["tree"], self.world["tree"])
                with self.assertRaisesRegex(Refusal, "cannot be overridden"):
                    reconstruct.recover(self.world, self.rows, recorded)
                with self.assertRaisesRegex(Refusal, "another combination"):
                    reconstruct.recover(self.world, [], recorded | {"snapshot": "e" * 64})

    def test_recovery_rejects_paths_native_claims_duplicate_files_and_unbounded_text(self):
        row = {"path": "Cargo.lock", "oid": "a" * 40, "edits": [{"before": "old", "after": "new"}]}
        for changed in (row | {"path": "../Cargo.lock"}, row | {"path": ".git/config"}, row | {"native": {}}, row | {"oid": "bad"}, row | {"edits": [{"before": "", "after": "new"}]}, row | {"edits": [{"before": "x" * 8193, "after": "new"}]}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                reconstruct.validate([changed])
        with self.assertRaises(Refusal):
            reconstruct.validate([row, copy.deepcopy(row)])

    def test_archive_refuses_foreign_repository_before_starting_git(self):
        with mock.patch.object(materials, "git") as git, self.assertRaises(Refusal):
            reconstruct.archived("/unused", {"repository": "Other/product", "commit": "a" * 40, "tree": "b" * 40})
        git.assert_not_called()
