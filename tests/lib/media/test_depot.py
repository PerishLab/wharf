import json
import unittest

from lib.media import depot, edit, lineage
from lib.refusal import Conflict, Refusal
from tests.lib.store.memory import Memory

OBJECTS = [
    {"path": "rules/policy.toml", "sha256": depot.sha(b"policy"), "size": 6, "mediaType": "application/toml", "executable": False},
    {"path": "assets/hook", "sha256": depot.sha(b"hook"), "size": 4, "mediaType": "application/octet-stream", "executable": True},
]
BODIES = {"rules/policy.toml": b"policy", "assets/hook": b"hook"}
BASE = "https://depot.plumb.perish.uk"


def release(marker="v0.38.0-beta.8", commit="a" * 40):
    return depot.Release("PerishLab/plumb", marker, commit, "b" * 40)


class Format(unittest.TestCase):
    def test_encodings_follow_serde(self):
        self.assertEqual(depot.compact({"b": 1, "a": "é"}), '{"b":1,"a":"é"}'.encode())
        self.assertEqual(depot.pretty({"a": [], "b": {"c": None}}), b'{\n  "a": [],\n  "b": {\n    "c": null\n  }\n}')

    def test_manifest_orders_objects_and_binds_the_release_digest(self):
        held = depot.identity(release(), "beta", "skill")
        document = depot.manifest(held, OBJECTS)
        self.assertEqual([entry["path"] for entry in document["objects"]], ["assets/hook", "rules/policy.toml"])
        self.assertEqual(list(document), ["format", "product", "channel", "version", "marker", "kind", "objects"])
        self.assertEqual(held["marker"]["name"], held["version"])
        self.assertRegex(held["marker"]["sha256"], "^[0-9a-f]{64}$")

    def test_pointer_names_its_generation_route(self):
        document = depot.manifest(depot.identity(release(), "beta", "skill"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-09-18T00:00:00Z")
        self.assertTrue(held["manifest"]["url"].endswith(f"/channels/beta/skills/versions/v0.38.0-beta.8/generations/{held['generation']}/manifest.json"))
        self.assertEqual(list(held)[-2:], ["previousGeneration", "createdAt"])

    def test_kind_selects_its_route_and_refuses_unknown_kinds(self):
        document = depot.manifest(depot.identity(release(), "beta", "skill"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-09-18T00:00:00Z")
        self.assertIn("/channels/beta/skills/versions/v0.38.0-beta.8/generations/", held["manifest"]["url"])
        with self.assertRaisesRegex(Refusal, "not a depot kind"):
            depot.route("beta", "v0.38.0-beta.8", "rules")

    def test_a_changelog_belongs_to_a_stable_marker_only(self):
        held = depot.identity(release("v0.38.0"), "stable", "changelog")
        self.assertEqual((held["channel"], held["kind"]), ("stable", "changelog"))
        with self.assertRaisesRegex(Refusal, "describes a stable release"):
            depot.identity(release("v0.38.0-rc.2"), "rc", "changelog")

    def test_channel_of_names_stable_for_an_exact_marker(self):
        self.assertEqual((depot.channel_of("v0.38.0"), depot.channel_of("v0.38.0-rc.1")), ("stable", "rc"))
        with self.assertRaises(Refusal):
            depot.channel_of("0.38.0")

    def test_scene_generation_is_a_separate_marker_projection(self):
        document = depot.manifest(depot.identity(release(), "beta", "scene"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-10-28T00:00:00Z")
        self.assertIn("/channels/beta/scenes/versions/v0.38.0-beta.8/generations/", held["manifest"]["url"])
        self.assertEqual(held["marker"], document["marker"])
        self.assertIsNone(depot.lineage(held, dict(document, kind="skill")))

    def test_configuration_generation_is_registered_and_isolated(self):
        self.assertEqual(depot.KINDS["configuration"], "configurations")
        self.assertEqual(depot.route("beta", "v0.38.0-beta.8", "configuration"), "channels/beta/configurations/versions/v0.38.0-beta.8")
        document = depot.manifest(depot.identity(release(), "beta", "configuration"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-11-25T00:00:00Z")
        self.assertIn("/channels/beta/configurations/versions/v0.38.0-beta.8/generations/", held["manifest"]["url"])
        self.assertEqual(held["marker"], document["marker"])
        self.assertIsNone(depot.lineage(held, dict(document, kind="scene")))


class ConfigurationLineage(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def publish(self, marker, body, now):
        entry = {"path": "policy.toml", "sha256": depot.sha(body), "size": len(body), "mediaType": "application/toml", "executable": False}
        rel = release(marker)
        base = lineage.parent(self.bucket, (depot.channel_of(marker), marker, "configuration"))
        staged = edit.stage(self.bucket, edit.Change(rel, "configuration", {"policy.toml": (entry, body)}, base, now))
        found = lineage.discovery(self.bucket, rel, "configuration", staged["generation"])
        return staged, found

    def test_discovery_refuses_kinds_outside_discovery_set(self):
        with self.assertRaisesRegex(Refusal, "does not publish a channel discovery pointer"):
            lineage.discovery(self.bucket, release("v1.0.0"), "skill", "0" * 64)

    def test_rollback_verifies_inputs_manifest_objects_and_cas_conflicts(self):
        first, _ = self.publish("v1.0.0", b"level = 1\n", "2026-11-25T00:00:00Z")
        second, _ = self.publish("v1.1.0", b"level = 2\n", "2026-11-25T00:01:00Z")
        rel = release("v1.0.0")
        with self.assertRaisesRegex(Refusal, "not a sha256"):
            lineage.rollback(self.bucket, rel, "bad", "2026-11-25T00:02:00Z")
        with self.assertRaisesRegex(Refusal, "timestamp is invalid"):
            lineage.rollback(self.bucket, rel, first["generation"], "not-a-timestamp")
        with self.assertRaisesRegex(Refusal, "does not exist"):
            lineage.rollback(self.bucket, rel, "0" * 64, "2026-11-25T00:02:00Z")
        with self.assertRaisesRegex(Refusal, "does not match target release identity"):
            lineage.rollback(self.bucket, release("v1.0.0", commit="c" * 40), first["generation"], "2026-11-25T00:02:00Z")

        folder = f"channels/stable/configurations/versions/v1.0.0/generations/{first['generation']}"
        original_object = self.bucket.get(f"{folder}/objects/policy.toml")
        self.bucket.put(f"{folder}/objects/policy.toml", b"tampered = true\n")
        with self.assertRaisesRegex(Refusal, "failed sha256 verification"):
            lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        del self.bucket.objects[f"{folder}/objects/policy.toml"]
        with self.assertRaisesRegex(Refusal, "is missing"):
            lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        self.bucket.put(f"{folder}/objects/policy.toml", original_object)

        original_manifest = self.bucket.get(f"{folder}/manifest.json")
        self.bucket.put(f"{folder}/manifest.json", b"{not-json")
        with self.assertRaisesRegex(Refusal, "not readable JSON"):
            lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        self.bucket.put(f"{folder}/manifest.json", b'{"tampered":true}')
        with self.assertRaisesRegex(Refusal, "does not hold the generation it names"):
            lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        self.bucket.put(f"{folder}/manifest.json", original_manifest)

        swap = self.bucket.swap
        self.bucket.swap = lambda key, body, etag: (_ for _ in ()).throw(Conflict(key))
        with self.assertRaisesRegex(Refusal, "moved during rollback"):
            lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        self.bucket.swap = swap

        outcome = lineage.rollback(self.bucket, rel, first["generation"], "2026-11-25T00:02:00Z")
        self.assertEqual(outcome, {"state": "rolled-back", "version": "v1.0.0", "generation": first["generation"], "previous": second["generation"]})
        current = json.loads(self.bucket.get("channels/stable/configurations/latest.json"))
        self.assertEqual((current["version"], current["generation"], current["previousGeneration"]), ("v1.0.0", first["generation"], second["generation"]))
