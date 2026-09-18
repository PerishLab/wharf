import json
import os
import tempfile
import unittest
from pathlib import Path

from lib.media import depot, edit, lineage
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

NOW = "2026-09-18T00:00:00Z"


def release(marker, commit="a" * 40):
    return depot.Release("PerishLab/plumb", marker, commit, "b" * 40)


def tree(files):
    root = Path(tempfile.mkdtemp()) / "tree"
    for relative, (body, mode) in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.chmod(path, mode)
    return root


class Edit(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        root = tree({"rules/policy.toml": (b"policy", 0o644), "assets/hook": (b"hook", 0o755)})
        self.first = edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "configuration", edit.directory(root), None, NOW))

    def test_full_publish_records_media_and_modes(self):
        own = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        entries = {entry["path"]: entry for entry in own["document"]["objects"]}
        self.assertEqual((entries["assets/hook"]["executable"], entries["rules/policy.toml"]["mediaType"]), (True, "application/toml"))
        self.assertIsNone(self.first["previous"])

    def test_patch_continues_lineage_and_copies_unchanged_objects(self):
        base = lineage.base(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        change = tree({"help/new.txt": (b"new", 0o644)})
        content = edit.patched(base, [("help/new.txt", change / "help/new.txt")], ["assets/hook"])
        result = edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "configuration", content, base, NOW))
        self.assertEqual(result["previous"], self.first["generation"])
        self.assertTrue(any(key.endswith("objects/rules/policy.toml") for key in self.bucket.copies))
        now = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        self.assertEqual(edit.diff(base, now), {"from": base["generation"], "to": now["generation"], "added": ["help/new.txt"], "removed": ["assets/hook"], "changed": []})

    def test_new_marker_inherits_the_nearest_on_its_line(self):
        base = lineage.base(self.bucket, ("beta", "v0.38.0-beta.9", "configuration"))
        self.assertEqual(base["version"], "v0.38.0-beta.8")
        result = edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.9"), "configuration", edit.patched(base, [], []), base, NOW))
        self.assertIsNone(result["previous"])
        with self.assertRaisesRegex(Refusal, "--full or --from"):
            lineage.base(self.bucket, ("beta", "v0.39.0-beta.1", "configuration"))

    def test_from_names_a_route_or_a_digest(self):
        by_route = lineage.base(self.bucket, ("beta", "v0.39.0-beta.1", "configuration"), "beta/v0.38.0-beta.8")
        by_digest = lineage.base(self.bucket, ("beta", "v0.39.0-beta.1", "configuration"), self.first["generation"])
        self.assertEqual(by_route["generation"], by_digest["generation"])

    def test_concurrent_pointer_moves_are_refused(self):
        base = lineage.base(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        content = edit.patched(base, [], ["assets/hook"])
        key = "channels/beta/configurations/versions/v0.38.0-beta.8/latest.json"
        original = self.bucket.swap
        self.bucket.swap = lambda k, body, etag: (self.bucket.put(key, b"{}"), original(k, body, etag))
        with self.assertRaisesRegex(Refusal, "pull again"):
            edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "configuration", content, base, NOW))

    def test_pull_materializes_bodies_and_modes(self):
        base = lineage.base(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        target = Path(tempfile.mkdtemp()) / "pulled"
        self.assertEqual(edit.pull(self.bucket, base, target)["objects"], 2)
        self.assertTrue(os.access(target / "assets/hook", os.X_OK))
        self.assertEqual(edit.directory(target).keys(), {"assets/hook", "rules/policy.toml"})

    def test_kinds_keep_separate_lineages(self):
        skill = tree({"SKILL.md": (b"skill", 0o644)})
        with self.assertRaisesRegex(Refusal, "no skill generation below"):
            lineage.base(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        result = edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "skill", edit.directory(skill), None, NOW))
        self.assertIsNone(result["previous"])
        own = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        self.assertEqual((own["document"]["kind"], own["folder"].split("/")[2]), ("skill", "skills"))
        configuration = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "configuration"))
        self.assertEqual(configuration["generation"], self.first["generation"])

    def test_version_order_puts_stable_after_its_prereleases(self):
        self.assertLess(lineage.order("v0.38.0-beta.9"), lineage.order("v0.38.0-rc.1"))
        self.assertLess(lineage.order("v0.38.0-rc.1"), lineage.order("v0.38.0"))
        self.assertLess(lineage.order("v0.38.0-beta.2"), lineage.order("v0.38.0-beta.10"))

    def test_rejects_unanchored_paths(self):
        with self.assertRaises(Refusal):
            edit.patched(None, [("../escape", "/dev/null")], [])
