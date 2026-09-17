import json
import os
import unittest
from unittest import mock

from lib.cargo import publish, version
from lib.refusal import Refusal
from tests.lib.cargo.test_basis import Repository

LOCATION = "https://git.perish.top/api/packages/PerishLab/cargo/"


class Registry:
    def __init__(self, released=()):
        self.released = set(released)
        self.runs = []

    def reader(self, url):
        return "\n".join(json.dumps({"vers": vers}) for name, vers in self.released if url.endswith("/" + name))

    def run(self, argv, cwd, env=None):
        self.runs.append((argv, env))
        self.released.add((argv[argv.index("--package") + 1], "1.2.3-beta.1"))
        return ""

    def tools(self):
        return publish.Tools(run=self.run, reader=self.reader, sleep=lambda seconds: None)


def publishing(repository):
    repository.write(".cargo/config.toml", f'[registries.perish]\nindex = "sparse+{LOCATION}"\n')
    repository.edit("crates/lib/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = ["perish"]')
    repository.edit("crates/cli/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = ["perish"]')
    repository.edit("crates/other/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = false')
    repository.commit()


class Publish(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        publishing(self.repository)

    def test_orders_by_workspace_dependency(self):
        self.assertEqual(publish.publishable(self.repository.root), [("demo", "perish"), ("demo-cli", "perish")])

    def test_publishes_pending_packages_in_order_with_token(self):
        registry = Registry(released={("demo", "1.2.3-beta.1")})
        with mock.patch.dict(os.environ, {publish.TOKEN: "secret"}):
            result = publish.publish(self.repository.root, "1.2.3-beta.1", registry.tools())
        self.assertEqual([item["state"] for item in result["packages"]], ["already-published", "published"])
        argv, env = registry.runs[0]
        self.assertIn("--no-verify", argv)
        self.assertEqual(argv[argv.index("--package") + 1], "demo-cli")
        self.assertEqual(env["CARGO_REGISTRIES_PERISH_TOKEN"], "Bearer secret")

    def test_refuses_without_token(self):
        with mock.patch.dict(os.environ, {publish.TOKEN: ""}), self.assertRaises(Refusal):
            publish.publish(self.repository.root, "1.2.3-beta.1", Registry().tools())

    def test_refuses_unknown_registry(self):
        self.repository.write(".cargo/config.toml", '[registries.perish]\nindex = "sparse+https://elsewhere/"\n')
        self.repository.commit()
        with self.assertRaises(Refusal):
            publish.pending(self.repository.root, "1.2.3-beta.1", Registry().reader)


class Inject(unittest.TestCase):
    def test_binds_every_unversioned_declaration(self):
        repository = Repository()
        self.assertEqual(version.inject(repository.root, "1.2.3-beta.1")["version"], "1.2.3-beta.1")
        self.assertIn('version = "=1.2.3-beta.1"', (repository.root / "crates/cli/Cargo.toml").read_text())
        self.assertIn('version = "1.2.3-beta.1"', (repository.root / "Cargo.toml").read_text())
        self.assertNotIn("0.0.0", (repository.root / "Cargo.lock").read_text())

    def test_marker_is_required(self):
        with self.assertRaises(Refusal):
            version.marker("latest")
        self.assertEqual(version.marker("v1.2.3-beta.1"), "1.2.3-beta.1")
