import os
import json
import unittest
from unittest import mock

from lib.media import lineage
from lib.refusal import Conflict, Refusal
from scripts import depot
from tests.lib.content.test_consigned import fake
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory
from tests.lib.store.test_yard import consign, entry


class Lodge(unittest.TestCase):
    def setUp(self):
        self.buckets = {}
        self.repository = Repository()
        for marker in ("v1.0.0", "v1.1.0"):
            self.repository.git("tag", "-a", marker, "-m", marker)
        patcher = mock.patch.object(depot.r2, "writer", side_effect=lambda name, role: self.buckets.setdefault(name, Memory()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def lodged(self, marker, kind, objects):
        yard = self.buckets.setdefault("perish-wharf-yard", Memory())
        held = consign(yard, objects, {"repository": "PerishLab/demo", "marker": marker, "kind": kind})
        return depot.lodge(dict(held, source=self.repository.root))

    def test_a_skill_lodges_once_and_again_changes_nothing(self):
        first = self.lodged("v1.0.0", "skill", [entry("SKILL.md", b"# demo\n")])
        self.assertEqual((first["state"], first["base"]), ("published", None))
        again = self.lodged("v1.0.0", "skill", [entry("SKILL.md", b"# demo\n")])
        self.assertEqual(again["state"], "already-published")

    def test_the_next_stable_changelog_descends_from_the_last(self):
        with mock.patch.dict(os.environ, fake(0)):
            first = self.lodged("v1.0.0", "changelog", [entry("en/INDEX.md", b"one")])
            second = self.lodged("v1.1.0", "changelog", [entry("en/INDEX.md", b"two")])
        self.assertEqual(second["base"], first["generation"])
        standing = lineage.standing(self.buckets["perish-demo-depot"], ("stable", "v1.1.0", "changelog"))
        self.assertEqual(standing["generation"], second["generation"])

    def test_a_skill_without_a_brief_writes_nothing_to_depot(self):
        with self.assertRaisesRegex(Refusal, "carries no SKILL.md"):
            self.lodged("v1.0.0", "skill", [entry("PATHS.md", b"# paths\n")])
        self.assertEqual(self.buckets.get("perish-demo-depot", Memory()).writes, [])

    def test_a_skill_with_a_companion_writes_nothing_to_depot(self):
        objects = [entry("SKILL.md", b"# demo\n"), entry("PATHS.md", b"# paths\n")]
        with self.assertRaisesRegex(Refusal, "entries beside SKILL.md"):
            self.lodged("v1.0.0", "skill", objects)
        self.assertEqual(self.buckets.get("perish-demo-depot", Memory()).writes, [])

    def test_a_scene_preserves_materials_without_running_them(self):
        objects = [entry("writer.toml", b"command = []\n"), entry("run.sh", b"exit 91\n", True)]
        first = self.lodged("v1.0.0", "scene", objects)
        store = self.buckets["perish-demo-depot"]
        standing = lineage.standing(store, ("stable", "v1.0.0", "scene"))
        self.assertEqual(standing["generation"], first["generation"])
        again = self.lodged("v1.0.0", "scene", objects)
        self.assertEqual(again["state"], "already-published")
        following = self.lodged("v1.0.0", "scene", [entry("writer.toml", b"changed")])
        self.assertEqual(following["base"], first["generation"])
        self.assertEqual(lineage.standing(store, ("stable", "v1.0.0", "scene"))["generation"], following["generation"])

    def test_a_scene_refuses_bad_paths_modes_and_digests_before_depot_writes(self):
        damaged = entry("writer.toml", b"bytes")
        damaged["sha256"] = "0" * 64
        mode = entry("writer.toml", b"bytes")
        mode["executable"] = "false"
        for objects in ([entry("../escape", b"bytes")], [entry("a\\b", b"bytes")], [damaged], [mode]):
            with self.subTest(objects=objects), self.assertRaises(Refusal):
                self.lodged("v1.0.0", "scene", objects)
            self.assertEqual(self.buckets.get("perish-demo-depot", Memory()).writes, [])

    def test_scene_discovery_follows_its_own_marker_and_generation(self):
        self.lodged("v1.0.0", "scene", [entry("case.toml", b"first")])
        second = self.lodged("v1.1.0", "scene", [entry("case.toml", b"second")])
        store = self.buckets["perish-demo-depot"]
        key = "channels/stable/scenes/latest.json"
        self.assertEqual(store.get(key), store.get("channels/stable/scenes/versions/v1.1.0/latest.json"))
        self.assertEqual(json.loads(store.get(key))["generation"], second["generation"])
        old = self.lodged("v1.0.0", "scene", [entry("case.toml", b"old marker edit")])
        self.assertEqual(old["discovery"]["state"], "newer")
        self.assertEqual(json.loads(store.get(key))["generation"], second["generation"])
        changed = self.lodged("v1.1.0", "scene", [entry("case.toml", b"new generation")])
        self.assertEqual(json.loads(store.get(key))["generation"], changed["generation"])

    def test_repeated_scene_consignment_repairs_failed_channel_publication(self):
        store = self.buckets.setdefault("perish-demo-depot", Memory())
        original = store.swap
        key = "channels/stable/scenes/latest.json"
        def refused(path, body, etag):
            if path == key:
                raise Conflict("injected channel conflict")
            return original(path, body, etag)
        objects = [entry("case.toml", b"scene")]
        with mock.patch.object(store, "swap", side_effect=refused):
            with self.assertRaisesRegex(Refusal, "channel moved"):
                self.lodged("v1.0.0", "scene", objects)
        self.assertTrue(store.exists("channels/stable/scenes/versions/v1.0.0/latest.json"))
        self.assertFalse(store.exists(key))
        repaired = self.lodged("v1.0.0", "scene", objects)
        self.assertEqual(repaired["state"], "already-published")
        self.assertEqual(repaired["discovery"]["state"], "published")
        self.assertEqual(store.get(key), store.get("channels/stable/scenes/versions/v1.0.0/latest.json"))

    def test_scene_discovery_refuses_foreign_and_corrupted_pointer_bindings(self):
        self.lodged("v1.0.0", "scene", [entry("case.toml", b"scene")])
        store = self.buckets["perish-demo-depot"]
        key = "channels/stable/scenes/latest.json"
        valid = store.get(key)
        foreign = json.loads(valid)
        foreign["product"] = "foreign"
        store.put(key, json.dumps(foreign).encode())
        with self.assertRaisesRegex(Refusal, "another projection"):
            self.lodged("v1.0.0", "scene", [entry("case.toml", b"scene")])
        corrupt = json.loads(valid)
        corrupt["manifest"]["sha256"] = "0" * 64
        store.put(key, json.dumps(corrupt).encode())
        with self.assertRaisesRegex(Refusal, "does not bind"):
            self.lodged("v1.0.0", "scene", [entry("case.toml", b"scene")])

        for field, value in (("format", 99), ("createdAt", None), ("previousGeneration", "invalid"), ("generation", "0" * 64)):
            with self.subTest(field=field):
                broken = json.loads(valid)
                broken[field] = value
                store.put(key, json.dumps(broken).encode())
                with self.assertRaises(Refusal):
                    self.lodged("v1.0.0", "scene", [entry("case.toml", b"scene")])

    def test_skill_does_not_publish_a_scene_discovery_pointer(self):
        self.lodged("v1.0.0", "skill", [entry("SKILL.md", b"# demo\n")])
        self.assertFalse(self.buckets["perish-demo-depot"].exists("channels/stable/scenes/latest.json"))
