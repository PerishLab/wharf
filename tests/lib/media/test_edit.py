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
        self.first = edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "skill", edit.directory(root), None, NOW))

    def test_full_publish_records_media_and_modes(self):
        own = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        entries = {entry["path"]: entry for entry in own["document"]["objects"]}
        self.assertEqual((entries["assets/hook"]["executable"], entries["rules/policy.toml"]["mediaType"]), (True, "application/toml"))
        self.assertIsNone(self.first["previous"])

    def lodge(self, marker, files):
        place = (depot.channel_of(marker), marker, "skill")
        base = lineage.parent(self.bucket, place)
        return edit.stage(self.bucket, edit.Change(release(marker), "skill", edit.directory(tree(files)), base, NOW)), base

    def test_a_second_generation_continues_lineage_and_copies_what_is_unchanged(self):
        result, base = self.lodge("v0.38.0-beta.8", {"rules/policy.toml": (b"policy", 0o644), "help/new.txt": (b"new", 0o644)})
        self.assertEqual((result["previous"], base["generation"]), (self.first["generation"], self.first["generation"]))
        self.assertTrue(any(key.endswith("objects/rules/policy.toml") for key in self.bucket.copies))
        own = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        self.assertEqual(sorted(entry["path"] for entry in own["document"]["objects"]), ["help/new.txt", "rules/policy.toml"])

    def test_a_new_marker_inherits_the_nearest_on_its_line(self):
        result, base = self.lodge("v0.38.0-beta.9", {"rules/policy.toml": (b"policy", 0o644), "assets/hook": (b"hook", 0o755)})
        self.assertEqual((base["version"], result["previous"]), ("v0.38.0-beta.8", None))
        own = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.9", "skill"))
        self.assertEqual(sorted(entry["path"] for entry in own["document"]["objects"]), ["assets/hook", "rules/policy.toml"])
        self.assertIsNone(lineage.parent(self.bucket, ("beta", "v0.39.0-beta.1", "skill")))

    def test_lodging_the_same_content_again_changes_nothing(self):
        result, _ = self.lodge("v0.38.0-beta.8", {"rules/policy.toml": (b"policy", 0o644), "assets/hook": (b"hook", 0o755)})
        self.assertEqual((result["generation"], result["state"]), (self.first["generation"], "already-published"))

    def test_concurrent_pointer_moves_are_refused(self):
        content = edit.directory(tree({"rules/policy.toml": (b"policy", 0o644)}))
        base = lineage.parent(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        key = "channels/beta/skills/versions/v0.38.0-beta.8/latest.json"
        original = self.bucket.swap
        self.bucket.swap = lambda k, body, etag: (self.bucket.put(key, b"{}"), original(k, body, etag))
        with self.assertRaisesRegex(Refusal, "pull again"):
            edit.stage(self.bucket, edit.Change(release("v0.38.0-beta.8"), "skill", content, base, NOW))

    def test_kinds_keep_separate_lineages(self):
        notes = tree({"en/INDEX.md": (b"notes", 0o644)})
        self.assertIsNone(lineage.parent(self.bucket, ("stable", "v0.38.0", "changelog")))
        result = edit.stage(self.bucket, edit.Change(release("v0.38.0"), "changelog", edit.directory(notes), None, NOW))
        self.assertIsNone(result["previous"])
        own = lineage.standing(self.bucket, ("stable", "v0.38.0", "changelog"))
        self.assertEqual((own["document"]["kind"], own["folder"].split("/")[2]), ("changelog", "changelogs"))
        standing = lineage.standing(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))
        self.assertEqual(standing["generation"], self.first["generation"])

    def test_a_parent_is_the_own_generation_else_the_line_else_the_last_stable(self):
        self.assertEqual(lineage.parent(self.bucket, ("beta", "v0.38.0-beta.8", "skill"))["generation"], self.first["generation"])
        self.assertEqual(lineage.parent(self.bucket, ("stable", "v0.38.0", "skill"))["generation"], self.first["generation"])
        self.assertIsNone(lineage.parent(self.bucket, ("stable", "v0.39.0", "skill")))
        stable, _ = self.lodge("v0.38.0", {"rules/policy.toml": (b"policy", 0o644)})
        self.assertEqual(lineage.parent(self.bucket, ("stable", "v0.39.0", "skill"))["generation"], stable["generation"])
        self.assertIsNone(lineage.parent(self.bucket, ("stable", "v0.37.0", "skill")))

    def test_version_order_puts_stable_after_its_prereleases(self):
        self.assertLess(lineage.order("v0.38.0-beta.9"), lineage.order("v0.38.0-rc.1"))
        self.assertLess(lineage.order("v0.38.0-rc.1"), lineage.order("v0.38.0"))
        self.assertLess(lineage.order("v0.38.0-beta.2"), lineage.order("v0.38.0-beta.10"))

