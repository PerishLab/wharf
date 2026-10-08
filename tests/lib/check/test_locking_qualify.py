import copy
import json
import unittest

from lib.check.locking.qualify import qualify, schema
from lib.refusal import Refusal
from tests.lib.check.test_locking_records import INTEGRITY


def fixture():
    return {"lockfileVersion": "9.0", "settings": {"autoInstallPeers": True, "excludeLinksFromLockfile": False}, "importers": {".": {"dependencies": {"demo": {"specifier": "1.2.3", "version": "1.2.3"}}}}, "packages": {"demo@1.2.3": {"resolution": {"integrity": INTEGRITY}}}, "snapshots": {"demo@1.2.3": {}}}


class QualifyTests(unittest.TestCase):
    def test_exact_bytes_compose_with_configuration(self):
        body = ("lockfileVersion: '9.0'\nsettings:\n  autoInstallPeers: true\n  excludeLinksFromLockfile: false\nimporters:\n  .:\n    dependencies:\n      demo:\n        specifier: 1.2.3\n        version: 1.2.3\npackages:\n  demo@1.2.3:\n    resolution: {integrity: " + INTEGRITY + "}\nsnapshots:\n  demo@1.2.3: {}\n").encode()
        manifests = {"package.json": json.dumps({"dependencies": {"demo": "1.2.3"}, "scripts": {"preinstall": "SECRET"}}).encode()}
        result = qualify(b"packages: []\n", manifests, body)
        self.assertEqual(result["files"]["pnpm-lock.yaml"].encode(), body)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(set(result["files"]), {".npmrc", "pnpm-lock.yaml", "pnpm-workspace.yaml"})
        edited = qualify(b"packages: []\n", manifests, body + b"\n")
        self.assertNotEqual(result["digests"], edited["digests"])

    def test_unknown_fields_at_every_interpreted_level(self):
        for path in [[], ["settings"], ["importers", "."], ["packages", "demo@1.2.3"], ["packages", "demo@1.2.3", "resolution"], ["snapshots", "demo@1.2.3"]]:
            lock = fixture()
            value = lock
            for key in path:
                value = value[key]
            value["runtime"] = "SECRET"
            with self.subTest(path=path), self.assertRaises(Refusal):
                schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})

    def test_dangling_graph_and_hidden_resolution(self):
        for field in ["packages", "snapshots", "importers"]:
            lock = fixture()
            lock[field] = {}
            with self.subTest(field=field), self.assertRaises(Refusal):
                schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})
        lock = fixture()
        lock["snapshots"]["demo@1.2.3"] = {"resolution": {"tarball": "https://foreign.invalid/x"}}
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})
        lock = fixture()
        lock["snapshots"]["demo@1.2.3"] = {"dependencies": {"foreign": "2.0.0"}}
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})

    def test_manifest_agreement_and_version_satisfaction(self):
        for manifest in [{}, {"dependencies": {"demo": "2.0.0"}}, {"devDependencies": {"demo": "1.2.3"}}, {"dependencies": {"other": "1.2.3"}}]:
            with self.subTest(manifest=manifest), self.assertRaises(Refusal):
                schema(fixture(), {".": manifest})
        lock = fixture()
        lock["importers"]["."]["dependencies"]["demo"]["specifier"] = "2.0.0"
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "2.0.0"}}})

    def test_workspace_links_resolve_only_supplied_named_manifests(self):
        lock = fixture()
        source = {".": {"dependencies": {"local": "workspace:*"}}, "packages/local": {"name": "local"}}
        lock["importers"] = {".": {"dependencies": {"local": {"specifier": "workspace:*", "version": "link:packages/local"}}}, "packages/local": {}}
        schema(lock, source)
        for reference in ["link:../packages/local", "link:/packages/local", "link:packages/foreign", "link:packages\\local", "link:packages/*", "1.2.3"]:
            broken = copy.deepcopy(lock)
            broken["importers"]["."]["dependencies"]["local"]["version"] = reference
            with self.subTest(reference=reference), self.assertRaises(Refusal):
                schema(broken, source)

    def test_unreferenced_record_still_checked(self):
        lock = fixture()
        lock["packages"]["foreign@1.0.0"] = {"resolution": {"integrity": INTEGRITY, "tarball": "https://foreign.invalid/x"}}
        lock["snapshots"]["foreign@1.0.0"] = {"optional": True}
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})

    def test_workspace_numeric_range_matches_local_version(self):
        lock = fixture()
        lock["importers"] = {".": {"dependencies": {"local": {"specifier": "workspace:^1.2.3", "version": "link:packages/local"}}}, "packages/local": {}}
        source = {".": {"dependencies": {"local": "workspace:^1.2.3"}}, "packages/local": {"name": "local", "version": "1.9.0"}}
        schema(lock, source)
        for version in [None, "2.0.0", "file:../x", "01.2.3"]:
            source["packages/local"]["version"] = version
            with self.subTest(version=version), self.assertRaises(Refusal):
                schema(lock, source)

    def test_missing_peer_record_and_collection_bounds(self):
        lock = fixture()
        key = "demo@1.2.3(peer@1.0.0)"
        lock["snapshots"] = {key: {}}
        lock["importers"]["."]["dependencies"]["demo"]["version"] = key.removeprefix("demo@")
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})
        lock = fixture()
        lock["packages"] = {f"demo{index}@1.0.0": {} for index in range(4097)}
        with self.assertRaises(Refusal):
            schema(lock, {".": {"dependencies": {"demo": "1.2.3"}}})
