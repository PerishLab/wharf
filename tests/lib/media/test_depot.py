import json
import unittest

from lib.media import depot
from lib.refusal import Refusal

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
        held = depot.identity(release(), "beta", "configuration")
        document = depot.manifest(held, OBJECTS)
        self.assertEqual([entry["path"] for entry in document["objects"]], ["assets/hook", "rules/policy.toml"])
        self.assertEqual(list(document), ["format", "product", "channel", "version", "marker", "kind", "objects"])
        self.assertEqual(held["marker"]["name"], held["version"])
        self.assertRegex(held["marker"]["sha256"], "^[0-9a-f]{64}$")

    def test_pointer_names_its_generation_route(self):
        document = depot.manifest(depot.identity(release(), "beta", "configuration"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-09-18T00:00:00Z")
        self.assertTrue(held["manifest"]["url"].endswith(f"/channels/beta/configurations/versions/v0.38.0-beta.8/generations/{held['generation']}/manifest.json"))
        self.assertEqual(list(held)[-2:], ["previousGeneration", "createdAt"])

    def test_kind_selects_its_route_and_refuses_unknown_kinds(self):
        document = depot.manifest(depot.identity(release(), "beta", "skill"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-09-18T00:00:00Z")
        self.assertIn("/channels/beta/skills/versions/v0.38.0-beta.8/generations/", held["manifest"]["url"])
        with self.assertRaisesRegex(Refusal, "not a depot kind"):
            depot.route("beta", "v0.38.0-beta.8", "rules")

    def test_channel_of_names_stable_for_an_exact_marker(self):
        self.assertEqual((depot.channel_of("v0.38.0"), depot.channel_of("v0.38.0-rc.1")), ("stable", "rc"))
        with self.assertRaises(Refusal):
            depot.channel_of("0.38.0")
