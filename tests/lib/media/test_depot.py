import json
import unittest

from lib.media import depot
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

OBJECTS = [
    {"path": "rules/policy.toml", "sha256": depot.sha(b"policy"), "size": 6, "mediaType": "application/toml", "executable": False},
    {"path": "assets/hook", "sha256": depot.sha(b"hook"), "size": 4, "mediaType": "application/octet-stream", "executable": True},
]
BODIES = {"rules/policy.toml": b"policy", "assets/hook": b"hook"}
BASE = "https://depot.plumb.perish.uk"


def release(marker="v0.38.0-beta.8", commit="a" * 40):
    return depot.Release("PerishLab/plumb", marker, commit, "b" * 40)


class Serving:
    def __init__(self, bucket):
        self.bucket = bucket

    def __call__(self, url):
        key = url.removeprefix(BASE + "/")
        return self.bucket.get(key) if self.bucket.exists(key) else None


class Format(unittest.TestCase):
    def test_encodings_follow_serde(self):
        self.assertEqual(depot.compact({"b": 1, "a": "é"}), '{"b":1,"a":"é"}'.encode())
        self.assertEqual(depot.pretty({"a": [], "b": {"c": None}}), b'{\n  "a": [],\n  "b": {\n    "c": null\n  }\n}')

    def test_manifest_orders_objects_and_binds_the_release_digest(self):
        held = depot.identity(release(), "beta")
        document = depot.manifest(held, OBJECTS)
        self.assertEqual([entry["path"] for entry in document["objects"]], ["assets/hook", "rules/policy.toml"])
        self.assertEqual(list(document), ["format", "product", "channel", "version", "marker", "kind", "objects"])
        self.assertEqual(held["marker"]["name"], held["version"])
        self.assertRegex(held["marker"]["sha256"], "^[0-9a-f]{64}$")

    def test_pointer_names_its_generation_route(self):
        document = depot.manifest(depot.identity(release(), "beta"), OBJECTS)
        held = depot.pointer(document, BASE, None, "2026-09-18T00:00:00Z")
        self.assertTrue(held["manifest"]["url"].endswith(f"/channels/beta/configurations/versions/v0.38.0-beta.8/generations/{held['generation']}/manifest.json"))
        self.assertEqual(list(held)[-2:], ["previousGeneration", "createdAt"])


class Publish(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.serving = Serving(self.bucket)

    def publish(self, marker="v0.38.0-beta.8", objects=OBJECTS, commit="a" * 40):
        carry = (objects, BODIES, "c" * 64)
        return depot.publish(self.bucket, depot.Publication(release(marker, commit), carry, "2026-09-18T00:00:00Z"), self.serving)

    def test_publishes_a_generation_the_reader_can_follow(self):
        result = self.publish()
        self.assertEqual((result["state"], result["previous"]), ("published", None))
        pointer = json.loads(self.bucket.get(result["pointer"]))
        folder = pointer["manifest"]["url"].removeprefix(BASE + "/").removesuffix("manifest.json")
        self.assertEqual(depot.sha(self.bucket.get(folder + "manifest.json")), pointer["manifest"]["sha256"])
        self.assertEqual(self.bucket.get(folder + "objects/assets/hook"), b"hook")

    def test_lineage_continues_and_restarts_on_a_rebound_marker(self):
        first = self.publish()
        self.assertEqual(self.publish()["state"], "already-published")
        changed = self.publish(objects=OBJECTS[:1])
        self.assertEqual(changed["previous"], first["generation"])
        rebound = self.publish(objects=OBJECTS[:1], commit="d" * 40)
        self.assertIsNone(rebound["previous"])

    def test_never_writes_stable(self):
        with self.assertRaises(Refusal):
            self.publish(marker="v0.38.0")
        self.assertEqual(self.bucket.writes, [])


class Carry(unittest.TestCase):
    def test_carries_a_verified_generation(self):
        bucket = Memory()
        serving = Serving(bucket)
        depot.publish(bucket, depot.Publication(release(), (OBJECTS, BODIES, "c" * 64), "2026-09-18T00:00:00Z"), serving)
        objects, bodies, generation = depot.carried(BASE, "beta", "v0.38.0-beta.8", serving)
        self.assertEqual(bodies, BODIES)
        key = next(key for key in bucket.objects if key.endswith("objects/assets/hook"))
        bucket.objects[key] = b"tampered"
        with self.assertRaises(Refusal):
            depot.carried(BASE, "beta", "v0.38.0-beta.8", serving)
