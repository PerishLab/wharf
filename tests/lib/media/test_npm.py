import json
import os
import unittest
from unittest import mock

from lib.media import npm
from lib.refusal import Refusal
from tests.lib.media.repository import Repository


class Registry:
    def __init__(self, versions=()):
        self.versions = set(versions)
        self.runs = []

    def reader(self, url):
        return {"versions": {version: {} for version in self.versions}} if url.endswith("@perishlab%2Flib") else {}

    def run(self, argv, cwd, env=None):
        self.runs.append((argv, env))
        if argv[:2] == ["pnpm", "publish"]:
            self.versions.add(json.loads((cwd / "packages/lib/package.json").read_text())["version"])
        return ""


class Npm(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def test_lists_public_workspace_packages_with_their_registry(self):
        held = npm.publishable(self.repository.root)
        self.assertEqual(held, [{"name": "@perishlab/lib", "path": "packages/lib/package.json", "registry": "https://npm.pkg.github.com/"}])

    def test_refuses_unknown_registries_and_declared_versions(self):
        cases = (
            {".npmrc": "@perishlab:registry=https://elsewhere/\n"},
            {"packages/lib/package.json": json.dumps({"name": "@perishlab/lib", "version": "1.0.0"})},
            {"packages/lib/package.json": json.dumps({"name": "unscoped", "version": "0.0.0"})},
        )
        for files in cases:
            with self.subTest(files), self.assertRaises(Refusal):
                npm.publishable(Repository(files).root)

    def test_channel_follows_the_prerelease(self):
        self.assertEqual(npm.channel("1.2.3-beta.4"), "beta")
        self.assertEqual(npm.channel("1.2.3"), "latest")

    def test_publishes_once_with_the_channel_tag(self):
        registry = Registry()
        tools = npm.Tools(run=registry.run, reader=registry.reader)
        with mock.patch.dict(os.environ, {npm.TOKEN: "secret"}):
            first = npm.publish(self.repository.root, "1.2.3-beta.4", tools)
            second = npm.publish(Repository().root, "1.2.3-beta.4", tools)
        self.assertEqual([item["state"] for item in first["packages"]], ["published"])
        self.assertEqual([item["state"] for item in second["packages"]], ["already-published"])
        argv, env = next(run for run in registry.runs if run[0][:2] == ["pnpm", "publish"])
        self.assertEqual(argv[argv.index("--tag") + 1], "beta")
        self.assertTrue(env["NPM_CONFIG_USERCONFIG"].endswith("npmrc"))
        self.assertEqual(json.loads((self.repository.root / "packages/lib/package.json").read_text())["version"], "1.2.3-beta.4")

    def test_refuses_to_publish_without_a_token(self):
        registry = Registry()
        with mock.patch.dict(os.environ, {npm.TOKEN: ""}), self.assertRaises(Refusal):
            npm.publish(self.repository.root, "1.2.3-beta.4", npm.Tools(run=registry.run, reader=registry.reader))
