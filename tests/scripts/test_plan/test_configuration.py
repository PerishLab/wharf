import json
import unittest
import urllib.error

from lib.content import canonical
from lib.content.static import evidence
from lib.identity.guard import configuration
from lib.media import depot
from lib.refusal import Refusal

BASE = configuration.BASE


def make_generation(version="v0.90.0", body=b"level = 1\n", prior=None, created="2026-11-25T00:00:00Z"):
    entry = {"path": "policy.toml", "sha256": depot.sha(body), "size": len(body), "mediaType": "application/toml", "executable": False}
    rel = depot.Release("PerishLab/plumb", version, "a" * 40, "b" * 40)
    manifest = depot.manifest(depot.identity(rel, "stable", "configuration"), [entry])
    pointer = depot.pointer(manifest, BASE, prior, created)
    raw_manifest = depot.pretty(manifest)
    return pointer, manifest, raw_manifest


def depot_world(channel_ptr, standing_ptr, manifests):
    routes = {
        f"{BASE}/channels/stable/configurations/latest.json": depot.pretty(channel_ptr),
        f"{BASE}/channels/stable/configurations/versions/{channel_ptr['version']}/latest.json": depot.pretty(standing_ptr),
    }
    routes.update(manifests)
    return routes


class Discover(unittest.TestCase):
    def test_happy_path_discovers_and_returns_pinned_descriptor(self):
        ptr, _, raw = make_generation()
        routes = depot_world(ptr, ptr, {ptr["manifest"]["url"]: raw})
        pinned = configuration.discover(routes.__getitem__)
        self.assertEqual(pinned, {"channel": "stable", "version": "v0.90.0", "generation": ptr["generation"], "manifest": ptr["manifest"]})

    def test_intra_version_rollback_allows_standing_with_newer_generation(self):
        older, _, raw_old = make_generation("v0.90.0", b"level = 1\n", None, "2026-11-25T00:00:00Z")
        newer, _, raw_new = make_generation("v0.90.0", b"level = 2\n", older["generation"], "2026-11-25T00:01:00Z")
        rolled = dict(older, previousGeneration=newer["generation"], createdAt="2026-11-25T00:02:00Z")
        routes = depot_world(rolled, newer, {rolled["manifest"]["url"]: raw_old, newer["manifest"]["url"]: raw_new})
        pinned = configuration.discover(routes.__getitem__)
        self.assertEqual(pinned["generation"], older["generation"])
        self.assertEqual(pinned["manifest"], older["manifest"])

    def test_cross_version_rollback_verifies_target_version_standing(self):
        old_ver, _, raw_old = make_generation("v0.89.0", b"level = 1\n", None, "2026-11-25T00:00:00Z")
        new_ver, _, _ = make_generation("v0.90.0", b"level = 2\n", None, "2026-11-25T00:01:00Z")
        rolled = dict(old_ver, previousGeneration=new_ver["generation"], createdAt="2026-11-25T00:02:00Z")
        routes = depot_world(rolled, old_ver, {rolled["manifest"]["url"]: raw_old})
        pinned = configuration.discover(routes.__getitem__)
        self.assertEqual(pinned["version"], "v0.89.0")
        self.assertEqual(pinned["generation"], old_ver["generation"])

    def test_unreachable_or_404_or_oversized_responses_fail_closed(self):
        ptr, _, raw = make_generation()
        routes = depot_world(ptr, ptr, {ptr["manifest"]["url"]: raw})
        for failure in (OSError("network down"), urllib.error.HTTPError(ptr["manifest"]["url"], 404, "Not Found", {}, None), KeyError("missing")):
            with self.subTest(failure=type(failure).__name__), self.assertRaises(Refusal):
                configuration.discover(lambda url, err=failure: (_ for _ in ()).throw(err))
        for url in list(routes):
            broken = dict(routes)
            del broken[url]
            with self.subTest(missing=url), self.assertRaises(Refusal):
                configuration.discover(broken.__getitem__)
            oversized = dict(routes, **{url: b" " * (evidence.LIMIT + 1)})
            with self.subTest(oversized=url), self.assertRaises(Refusal):
                configuration.discover(oversized.__getitem__)

    def test_pointer_schema_and_identity_violations_fail_closed(self):
        ptr, _, raw = make_generation()
        bad_pointers = [
            dict(ptr, format=2),
            dict(ptr, format=True),
            dict(ptr, product="concord"),
            dict(ptr, channel="rc"),
            dict(ptr, version="v0.90.0-rc.1"),
            dict(ptr, kind="scene"),
            dict(ptr, generation="G" * 64),
            dict(ptr, createdAt="not-a-time"),
            dict(ptr, createdAt="2026-02-31T00:00:00Z"),
            dict(ptr, previousGeneration="short"),
            dict(ptr, marker={"name": "v0.91.0", "sha256": ptr["marker"]["sha256"]}),
            dict(ptr, manifest=dict(ptr["manifest"], url="https://evil.invalid/manifest.json")),
            dict(ptr, manifest=dict(ptr["manifest"], size=0)),
            dict(ptr, manifest=dict(ptr["manifest"], size=True)),
            dict(ptr, manifest=dict(ptr["manifest"], sha256="A" * 64)),
            dict(ptr, extra="field"),
        ]
        for index, bad in enumerate(bad_pointers):
            routes = depot_world(bad, ptr, {ptr["manifest"]["url"]: raw})
            routes[f"{BASE}/channels/stable/configurations/versions/{bad['version']}/latest.json"] = depot.pretty(ptr)
            with self.subTest(index=index), self.assertRaises(Refusal):
                configuration.discover(routes.__getitem__)

    def test_standing_pointer_disagreement_fails_closed(self):
        ptr, _, raw = make_generation()
        bad_standings = [
            dict(ptr, marker={"name": "v0.90.0", "sha256": "0" * 64}),
            dict(ptr, manifest=dict(ptr["manifest"], size=ptr["manifest"]["size"] + 1)),
            dict(ptr, product="concord"),
        ]
        for bad in bad_standings:
            routes = depot_world(ptr, bad, {ptr["manifest"]["url"]: raw})
            with self.subTest(bad=bad), self.assertRaises(Refusal):
                configuration.discover(routes.__getitem__)

    def test_tampered_manifest_bad_digest_size_objects_or_unanchored_paths_fail_closed(self):
        ptr, manifest, raw = make_generation()
        routes = depot_world(ptr, ptr, {ptr["manifest"]["url"]: raw + b" "})
        with self.assertRaises(Refusal):
            configuration.discover(routes.__getitem__)
        routes = depot_world(ptr, ptr, {ptr["manifest"]["url"]: b"x" * len(raw)})
        with self.assertRaises(Refusal):
            configuration.discover(routes.__getitem__)
        variants = [
            dict(manifest, objects=[]),
            dict(manifest, objects=[dict(manifest["objects"][0], path="../escape.toml")]),
            dict(manifest, objects=[dict(manifest["objects"][0], path="/abs.toml")]),
            dict(manifest, objects=[dict(manifest["objects"][0], path="a\\b.toml")]),
            dict(manifest, objects=[dict(manifest["objects"][0], path="b.toml"), dict(manifest["objects"][0], path="a.toml")]),
            dict(manifest, objects=[manifest["objects"][0], manifest["objects"][0]]),
            dict(manifest, objects=[dict(manifest["objects"][0], size=-1)]),
            dict(manifest, objects=[dict(manifest["objects"][0], executable="false")]),
        ]
        for changed in variants:
            tampered_raw = depot.pretty(changed)
            tampered_gen = depot.sha(depot.compact(changed))
            tampered_ptr = dict(ptr, generation=tampered_gen, manifest={"url": f"{BASE}/channels/stable/configurations/versions/v0.90.0/generations/{tampered_gen}/manifest.json", "sha256": depot.sha(tampered_raw), "size": len(tampered_raw)})
            bad_routes = depot_world(tampered_ptr, tampered_ptr, {tampered_ptr["manifest"]["url"]: tampered_raw})
            with self.subTest(changed=changed["objects"]), self.assertRaises(Refusal):
                configuration.discover(bad_routes.__getitem__)
        non_pretty = json.dumps(manifest, separators=(",", ":")).encode()
        mismatched_ptr = dict(ptr, manifest=dict(ptr["manifest"], sha256=depot.sha(non_pretty), size=len(non_pretty)))
        with self.assertRaises(Refusal):
            configuration.discover(depot_world(mismatched_ptr, mismatched_ptr, {mismatched_ptr["manifest"]["url"]: non_pretty}).__getitem__)


class VerifyAndParse(unittest.TestCase):
    def test_verify_succeeds_even_after_floating_latest_moves_to_newer_generation(self):
        first, _, raw_first = make_generation("v0.90.0", b"level = 1\n", None, "2026-11-25T00:00:00Z")
        second, _, raw_second = make_generation("v0.91.0", b"level = 2\n", first["generation"], "2026-11-25T00:05:00Z")
        pinned = {"channel": "stable", "version": "v0.90.0", "generation": first["generation"], "manifest": first["manifest"]}
        fetched = []

        def reader(url):
            fetched.append(url)
            if url == first["manifest"]["url"]:
                return raw_first
            if url.endswith("latest.json"):
                return depot.pretty(second)
            raise KeyError(url)

        self.assertEqual(configuration.verify(pinned, reader), pinned)
        self.assertEqual(fetched, [first["manifest"]["url"]])

    def test_verify_fails_closed_on_tampered_manifest_or_malformed_pinned_descriptor(self):
        ptr, _, raw = make_generation()
        pinned = {"channel": "stable", "version": "v0.90.0", "generation": ptr["generation"], "manifest": ptr["manifest"]}
        with self.assertRaises(Refusal):
            configuration.verify(pinned, lambda url: b"tampered")
        with self.assertRaises(Refusal):
            configuration.verify(pinned, lambda url: (_ for _ in ()).throw(OSError("down")))
        bad_pinned = [
            None,
            {},
            dict(pinned, channel="rc"),
            dict(pinned, version="v0.90.0-rc.1"),
            dict(pinned, generation="0" * 64),
            dict(pinned, manifest=dict(pinned["manifest"], url="https://other.invalid/manifest.json")),
            dict(pinned, extra="x"),
        ]
        for bad in bad_pinned:
            with self.subTest(bad=bad), self.assertRaises(Refusal):
                configuration.verify(bad, lambda url: raw)

    def test_parse_and_from_env_decode_strictly_and_raise_value_error_on_malformed(self):
        ptr, _, _ = make_generation()
        pinned = {"channel": "stable", "version": "v0.90.0", "generation": ptr["generation"], "manifest": ptr["manifest"]}
        encoded = canonical.encode(pinned).decode()
        self.assertIsNone(configuration.parse(None))
        self.assertIsNone(configuration.parse(""))
        self.assertIsNone(configuration.parse("null"))
        self.assertEqual(configuration.parse(encoded), pinned)
        self.assertIsNone(configuration.from_env({}))
        self.assertIsNone(configuration.from_env({configuration.VARIABLE: "null"}))
        self.assertIsNone(configuration.from_env({configuration.VARIABLE: ""}))
        self.assertEqual(configuration.from_env({configuration.VARIABLE: encoded}), pinned)
        for bad in ("{not-json", "123", "[]", "{}", " ", canonical.encode(dict(pinned, channel="beta")).decode()):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                configuration.parse(bad)
            with self.subTest(env=bad), self.assertRaises(ValueError):
                configuration.from_env({configuration.VARIABLE: bad})
        with self.assertRaises(ValueError):
            configuration.parse(123)
