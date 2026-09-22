import os
import unittest
from unittest import mock

from lib.media import lineage
from lib.refusal import Refusal
from scripts import depot
from tests.lib.content.test_consigned import fake
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory
from tests.lib.store.test_yard import consign, entry


class Lodge(unittest.TestCase):
    def setUp(self):
        self.buckets = {}
        self.repository = Repository({"skills/demo/SKILL.md": "# demo\n"})
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

    def test_a_drifted_skill_writes_nothing_to_depot(self):
        with self.assertRaisesRegex(Refusal, "differs from skills/demo"):
            self.lodged("v1.0.0", "skill", [entry("SKILL.md", b"# other\n")])
        self.assertEqual(self.buckets.get("perish-demo-depot", Memory()).writes, [])
