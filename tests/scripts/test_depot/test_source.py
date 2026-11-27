import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from lib.media import lineage
from lib.process import git
from lib.refusal import Refusal
from scripts import depot
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory


class Source(unittest.TestCase):
    def setUp(self):
        self.buckets = {}
        self.repository = Repository(
            {
                "scenes/bundle.toml": "version = 1\n",
                "scenes/run.sh": "#!/bin/sh\nexit 0\n",
                "rules/policy.toml": "enabled = true\n",
            }
        )
        os.chmod(self.repository.root / "scenes/run.sh", 0o755)
        self.repository.commit()
        self.v1 = self.tag("v1.0.0")
        self.repository.write("scenes/bundle.toml", "version = 2\n")
        self.repository.write("rules/policy.toml", "enabled = false\n")
        self.repository.commit()
        self.v2 = self.tag("v1.1.0")
        patcher = mock.patch.object(
            depot.r2,
            "writer",
            side_effect=lambda name, role: self.buckets.setdefault(name, Memory()),
        )
        self.writer = patcher.start()
        self.addCleanup(patcher.stop)

    def tag(self, marker):
        self.repository.git("tag", "-a", marker, "-m", marker)
        return git(self.repository.root, "rev-parse", f"refs/tags/{marker}^{{commit}}")

    def run_source(self, marker, kind, binding):
        commit, target = binding
        return depot.source(
            {
                "repository": "PerishLab/demo",
                "marker": marker,
                "source": str(self.repository.root),
                "kind": kind,
                "commit": commit,
                "target": target,
            }
        )

    def test_scene_lodges_from_immutable_tag_without_yard_and_verifies_readback(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        first = self.run_source("v1.0.0", "scene", (self.v1, "scenes"))
        self.assertEqual((first["state"], first["base"], first["verified"]), ("published", None, True))
        self.assertEqual((first["commit"], first["material"]), (self.v1, "scenes"))
        self.assertNotIn("perish-wharf-yard", self.buckets)
        store = self.buckets["perish-demo-depot"]
        standing = lineage.standing(store, ("stable", "v1.0.0", "scene"))
        self.assertEqual(standing["generation"], first["generation"])
        self.assertEqual(
            store.get("channels/stable/scenes/latest.json"),
            store.get("channels/stable/scenes/versions/v1.0.0/latest.json"),
        )
        objects = {item["path"]: item for item in standing["document"]["objects"]}
        self.assertTrue(objects["run.sh"]["executable"])
        self.assertFalse(objects["bundle.toml"]["executable"])

        again = self.run_source("v1.0.0", "scene", (self.v1, "scenes"))
        self.assertEqual((again["state"], again["discovery"]["state"], again["verified"]), ("already-published", "already-published", True))

        self.repository.git("checkout", "-q", "v1.1.0")
        second = self.run_source("v1.1.0", "scene", (self.v2, "scenes"))
        self.assertEqual((second["state"], second["base"], second["discovery"]["state"]), ("published", first["generation"], "published"))
        self.assertEqual(json.loads(store.get("channels/stable/scenes/latest.json"))["generation"], second["generation"])

        self.repository.git("checkout", "-q", "v1.0.0")
        older = self.run_source("v1.0.0", "scene", (self.v1, "scenes"))
        self.assertEqual((older["state"], older["discovery"]["state"], older["verified"]), ("already-published", "newer", True))
        self.assertEqual(json.loads(store.get("channels/stable/scenes/latest.json"))["generation"], second["generation"])

    def test_configuration_lodges_from_immutable_tag_and_refuses_invalid_toml(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        first = self.run_source("v1.0.0", "configuration", (self.v1, "rules"))
        self.assertEqual((first["state"], first["verified"], first["material"]), ("published", True, "rules"))
        self.assertNotIn("perish-wharf-yard", self.buckets)
        store = self.buckets["perish-demo-depot"]
        self.assertEqual(json.loads(store.get("channels/stable/configurations/latest.json"))["generation"], first["generation"])

        self.repository.git("checkout", "-q", "v1.1.0")
        self.repository.write("broken/policy.toml", "bad = \n")
        self.repository.commit()
        bad_commit = self.tag("v1.2.0")
        with self.assertRaisesRegex(Refusal, "not valid TOML"):
            self.run_source("v1.2.0", "configuration", (bad_commit, "broken"))
        self.assertFalse(store.exists("channels/stable/configurations/versions/v1.2.0/latest.json"))

    def test_refuses_non_tag_native_kinds_before_r2(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        for kind in ("changelog", "skill"):
            with self.subTest(kind=kind), self.assertRaisesRegex(Refusal, "only supported for scene and configuration"):
                self.run_source("v1.0.0", kind, (self.v1, "scenes"))
        self.assertEqual(self.buckets, {})

    def test_refuses_mismatched_foreign_or_dirty_commit_and_tree(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        with self.assertRaisesRegex(Refusal, "40-char"):
            self.run_source("v1.0.0", "scene", ("not-a-sha", "scenes"))
        with self.assertRaisesRegex(Refusal, "resolves to"):
            self.run_source("v1.0.0", "scene", (self.v2, "scenes"))
        self.repository.git("checkout", "-q", "v1.1.0")
        with self.assertRaisesRegex(Refusal, "does not match immutable tag"):
            self.run_source("v1.0.0", "scene", (self.v1, "scenes"))
        self.repository.git("checkout", "-q", "v1.0.0")
        (self.repository.root / "scenes/untracked.toml").write_text("extra = 1\n")
        with self.assertRaisesRegex(Refusal, "outside immutable tag"):
            self.run_source("v1.0.0", "scene", (self.v1, "scenes"))
        self.assertEqual(self.buckets, {})

    def test_refuses_unsafe_missing_or_symlink_material_root(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        for bad in ("/etc", "../outside", "a\\b", ".", "missing"):
            with self.subTest(target=bad), self.assertRaises(Refusal):
                self.run_source("v1.0.0", "scene", (self.v1, bad))
        os.symlink(self.repository.root / "scenes", self.repository.root / "linked")
        self.repository.commit()
        linked = self.tag("v1.0.1")
        with self.assertRaisesRegex(Refusal, "symlink"):
            self.run_source("v1.0.1", "scene", (linked, "linked"))
        self.assertEqual(self.buckets, {})

    def test_readback_refuses_corrupted_bytes_or_tampered_pointer(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        original = depot.edit.stage
        def tamper_bytes(store, change):
            result = original(store, change)
            key = f"channels/stable/scenes/versions/v1.0.0/generations/{result['generation']}/objects/bundle.toml"
            store.put(key, b"corrupted\n")
            return result
        with mock.patch.object(depot.edit, "stage", side_effect=tamper_bytes):
            with self.assertRaisesRegex(Refusal, "readback bytes"):
                self.run_source("v1.0.0", "scene", (self.v1, "scenes"))

    def test_depot_main_dispatches_source_action_and_workflow_declares_it(self):
        self.repository.git("checkout", "-q", "v1.0.0")
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "source",
            "--repository",
            "PerishLab/demo",
            "--marker",
            "v1.0.0",
            "--source",
            str(self.repository.root),
            "--kind",
            "scene",
            "--commit",
            self.v1,
            "--target",
            "scenes",
        ]
        with mock.patch.object(depot.sys, "stdout", stdout), mock.patch.object(depot.sys, "stderr", stderr):
            code = depot.main(argv)
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual((payload["state"], payload["commit"], payload["material"], payload["verified"]), ("published", self.v1, "scenes", True))
        workflow = (Path(git(".", "rev-parse", "--show-toplevel")) / ".github/workflows/depot.yml").read_text()
        self.assertIn("python3 -B -m scripts.depot source", workflow)
        self.assertNotIn("PLUMB_YARD", workflow)
