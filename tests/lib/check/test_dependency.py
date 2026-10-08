import json
import unittest

from lib.check import dependency as configuration
from lib.refusal import Refusal


class ConfigurationTests(unittest.TestCase):
    def qualify(self, workspace=b"packages: []\n", value=None):
        body = json.dumps({} if value is None else value).encode()
        return configuration.qualify(workspace, {"package.json": body})

    def test_fixed_projection_excludes_product_data(self):
        value = {"scripts": {"preinstall": "throw PRIVATE_SENTINEL"}, "dependencies": {"@perishlab/crest": "0", "demo": "workspace:*"}}
        result = self.qualify(b"packages:\n  - 'packages/*'\n", value)
        self.assertEqual(result["files"]["pnpm-workspace.yaml"], "packages: []\n")
        self.assertEqual(set(result["files"]), {"pnpm-workspace.yaml", ".npmrc"})
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        self.assertIn("@perishlab:registry=https://npm.pkg.github.com/\n", result["files"][".npmrc"])

    def test_digest_binds_exact_bytes_not_projection(self):
        first = configuration.qualify(b"packages: []\n", {"package.json": b"{}"})
        second = configuration.qualify(b"packages: []", {"package.json": b"{}\n"})
        self.assertNotEqual(first["digests"], second["digests"])
        self.assertEqual(first["files"], second["files"])

    def test_workspace_refuses_configuration_and_unsafe_selectors(self):
        values = [b"packages: []\npackages: []", b"packages: []\nconfigDependencies: secret", b"packages:\n  - ../secret", b"packages:\n  - /secret", b"packages:\n  - a\\secret", b"packages:\n  - ${SECRET}", b"packages:\n  - *secret", b"packages:\n  - a\n  - a", b"packages:\n  - a\nregistry: secret", b"packages:\n  - !!str secret", b"packages:\n  - a # secret", b"packages:\n  - [secret]", b"\xff"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(Refusal) as caught:
                self.qualify(value)
            self.assertNotIn("secret", str(caught.exception))

    def test_forbidden_manifest_configuration(self):
        for name in configuration.FORBIDDEN:
            with self.subTest(name=name), self.assertRaises(Refusal):
                self.qualify(value={name: {"secret": "value"}})

    def test_dependency_protocols_refuse(self):
        for value in ["file:../secret", "link:secret", "https://secret", "git+https://secret", "npm:secret@1", "catalog:", "workspace:../secret", "${SECRET}", "latest", "*", None, {}]:
            with self.subTest(value=value), self.assertRaises(Refusal):
                self.qualify(value={"dependencies": {"demo": value}})

    def test_literal_versions_and_workspace_links(self):
        for value in ["0", "2.5.3", "^1.2.3", "~1.2.3", "1.2.3-rc.1", "workspace:*", "workspace:^", "workspace:~", "workspace:^1.2.3"]:
            with self.subTest(value=value):
                self.qualify(value={"dependencies": {"demo": value}})

    def test_malformed_and_duplicate_manifest(self):
        for body in [b"[]", b"null", b"{", b' {"dependencies": {}, "dependencies": {}}', b'{"scripts": {"a": 1, "a": 2}}', b'{"value": NaN}', b"\xff"]:
            with self.subTest(body=body), self.assertRaises(Refusal):
                configuration.qualify(b"packages: []", {"package.json": body})

    def test_manifest_paths_and_input_bounds(self):
        for name in ["../package.json", "/package.json", "a//package.json", "a/./package.json", "a\\package.json", "*/package.json", "other.json"]:
            with self.subTest(name=name), self.assertRaises(Refusal):
                configuration.qualify(b"packages: []", {"package.json": b"{}", name: b"{}"})
        for manifests in [{}, {"a/package.json": b"{}"}, {"package.json": "{}"}, {"package.json": b" " * (configuration.LIMIT + 1)}]:
            with self.subTest(manifests=type(manifests)), self.assertRaises(Refusal):
                configuration.qualify(b"packages: []", manifests)

    def test_dependency_shapes_and_names(self):
        for value in [{"dependencies": []}, {"dependencies": {"bad/name": "0"}}, {"dependencies": {"@bad": "0"}}, {"dependencies": {"secret": 1}}]:
            with self.subTest(value=value), self.assertRaises(Refusal):
                self.qualify(value=value)

    def test_file_count_and_aggregate_bounds(self):
        manifests = {f"p{i}/package.json": b"{}" for i in range(configuration.FILES)}
        manifests["package.json"] = b"{}"
        with self.assertRaises(Refusal):
            configuration.qualify(b"packages: []", manifests)
        body = b" " * (configuration.LIMIT // 2) + b"{}"
        with self.assertRaises(Refusal):
            configuration.qualify(b"packages: []", {"package.json": body, "p/package.json": body})
