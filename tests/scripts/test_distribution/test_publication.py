import json
import unittest
from unittest import mock

from lib.content import canonical
from lib.process.trial import publication
from lib.refusal import Conflict, Refusal
from tests.scripts.test_distribution.world import AUTHORITY, OBSERVER, World


class Discovery(unittest.TestCase):
    def test_pending_new_attempt_replaces_old_green_without_erasing_history(self):
        world = World()
        material = {name: "a" * 64 for name in ("key", "record", "basis")}
        with world.wired():
            first = publication.begin(world.public, AUTHORITY, world.prepared, material)
            publication.finish(world.public, AUTHORITY, first, ({"ok": True, "state": "passed"}, {"old": "green"}))
            second = dict(world.prepared, observer=dict(OBSERVER, attempt="2"))
            publication.begin(world.public, AUTHORITY, second, material)
            index_key = publication.location("PerishLab/plumb", world.source["marker"])[2] + "/latest.json"
            self.assertEqual(json.loads(world.public.get(index_key))["observer"]["attempt"], "2")
            self.assertFalse(world.public.exists(publication.path("PerishLab/plumb", world.source["marker"], second["observer"]) + "/pass.json"))
            publication.begin(world.public, AUTHORITY, world.prepared, material)
            self.assertEqual(json.loads(world.public.get(index_key))["observer"]["attempt"], "2")
            self.assertTrue(world.public.exists(publication.path("PerishLab/plumb", world.source["marker"], OBSERVER) + "/pass.json"))

    def test_failures_never_publish_pass_and_diagnostics_do_not_replace_suite(self):
        world = World()
        material = {name: "a" * 64 for name in ("key", "record", "basis")}
        with world.wired():
            intent = publication.begin(world.public, AUTHORITY, world.prepared, material)
            result = publication.finish(world.public, AUTHORITY, intent, ({"ok": False, "state": "failed"}, None))
            self.assertIsNone(result["credential"])
            root = publication.path("PerishLab/plumb", world.source["marker"], OBSERVER)
            self.assertFalse(world.public.exists(root + "/pass.json"))
            diagnostic = dict(world.prepared, observer=dict(OBSERVER, attempt="2"), requirements=dict(world.prepared["requirements"], scope="diagnostic", selected=["fault"]))
            publication.begin(world.public, AUTHORITY, diagnostic, material)
            index = publication.location("PerishLab/plumb", world.source["marker"])[2] + "/latest.json"
            self.assertEqual(json.loads(world.public.get(index))["observer"], OBSERVER)

    def test_later_retry_of_older_run_is_not_hidden_by_newer_run_id(self):
        world = World()
        material = {name: "a" * 64 for name in ("key", "record", "basis")}
        with world.wired():
            publication.begin(world.public, AUTHORITY, world.prepared, material)
            retried = dict(world.prepared, observer=dict(OBSERVER, run="19", attempt="2"), started="2026-10-29T15:01:00Z")
            publication.begin(world.public, AUTHORITY, retried, material)
            index = publication.location("PerishLab/plumb", world.source["marker"])[2] + "/latest.json"
            self.assertEqual(json.loads(world.public.get(index))["observer"], retried["observer"])

    def test_one_attempt_cannot_change_intent_or_result(self):
        world = World()
        material = {name: "a" * 64 for name in ("key", "record", "basis")}
        with world.wired():
            intent = publication.begin(world.public, AUTHORITY, world.prepared, material)
            with self.assertRaises(Refusal):
                publication.begin(world.public, AUTHORITY, world.prepared, dict(material, key="b" * 64))
            publication.finish(world.public, AUTHORITY, intent, ({"state": "unknown"}, None))
            with self.assertRaises(Refusal):
                publication.finish(world.public, AUTHORITY, intent, ({"state": "passed"}, None))

    def test_cas_conflict_never_overwrites_without_rechecking(self):
        world = World()
        value = {"schema": "wharf.windtun.index/v1", "observer": OBSERVER, "started": world.prepared["started"], "intent": {}}
        original = world.public.swap
        calls = []

        def swap(key, body, etag):
            calls.append(key)
            if len(calls) == 1:
                world.public.objects[key] = canonical.encode(dict(value, observer=dict(OBSERVER, run="21")))
                raise Conflict("concurrent advance")
            original(key, body, etag)

        with mock.patch.object(world.public, "swap", side_effect=swap):
            self.assertEqual(publication.advance(world.public, "latest.json", value), "kept")
        self.assertEqual(len(calls), 1)

    def test_foreign_reference_is_refused_before_fetch(self):
        world = World()
        entry = {"url": "https://foreign.test/material", "sha256": "a" * 64, "size": 1}
        with mock.patch.object(publication.release, "fetch") as fetch, self.assertRaises(Refusal):
            publication.verified(entry, AUTHORITY)
        fetch.assert_not_called()
