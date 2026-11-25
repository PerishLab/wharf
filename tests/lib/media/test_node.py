import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from lib.media import node
from lib.refusal import Refusal
from tests.lib.media.repository import ROOT, Repository


class Engines(unittest.TestCase):
    def test_expects_the_domain_versions_whatever_the_manifest_declares(self):
        for root in (ROOT, {"name": "demo", "private": True}, dict(ROOT, engines={"node": ">=24"})):
            with self.subTest(root):
                self.assertEqual(node.expected(Repository({"package.json": json.dumps(root)}).root), {"node": "24.18.0", "pnpm": "11.28.5"})

    def test_refuses_a_package_manager(self):
        with self.assertRaisesRegex(Refusal, "packageManager"):
            node.expected(Repository({"package.json": json.dumps(dict(ROOT, packageManager="pnpm@11.28.5"))}).root)

    def test_basis_follows_the_tree(self):
        repository = Repository()
        before = node.basis(repository.root, "ubuntu-24.04")
        repository.write("docs/a.md", "x\n")
        repository.commit()
        self.assertNotEqual(node.basis(repository.root, "ubuntu-24.04")["tree"], before["tree"])


class Carried(unittest.TestCase):
    def test_a_repository_that_tracks_a_manifest_carries_a_node_workspace(self):
        self.assertTrue(node.carried(Repository().root))

    def test_a_repository_without_one_carries_none_and_is_not_refused_for_asking(self):
        repository = Repository()
        repository.git("rm", "-q", "package.json")
        repository.commit()
        self.assertFalse(node.carried(repository.root))
        with self.assertRaises(Refusal):
            node.expected(repository.root)


class Suite(unittest.TestCase):
    def runner(self, declared=""):
        return lambda argv, cwd: declared if "config" in argv else {"node": "v24.18.0\n", "pnpm": "11.28.5\n"}[argv[0]]

    def test_requires_the_prepared_toolchain_and_runs_both_steps(self):
        repository = Repository()
        calls = []
        receipt = node.suite(node.Suite(repository.root, repository.root / "out"), self.runner('["svelte@5.57.1"]\n'), lambda argv, cwd, env: calls.append((argv, env["HOME"])))
        expected = ["pnpm", "install", "--frozen-lockfile", "--config.minimum-release-age-exclude=svelte@5.57.1", "--config.minimum-release-age-exclude=@perishlab/*"]
        self.assertEqual(receipt["toolchain"], {"node": "24.18.0", "pnpm": "11.28.5"})
        self.assertEqual(receipt["commands"], [" ".join(expected), "pnpm -r test"])
        self.assertEqual([argv for argv, _ in calls], [expected, ["pnpm", "-r", "test"]])
        self.assertNotEqual(calls[0][1], str(Path.home()))

    def test_exclusions_preserve_declared_patterns_and_refuse_malformed_config(self):
        repository = Repository()
        self.assertEqual(node.exclusions(repository.root, self.runner('"@perishlab/*"\n')), ["--config.minimum-release-age-exclude=@perishlab/*"])
        self.assertEqual(node.exclusions(repository.root, self.runner("null\n")), ["--config.minimum-release-age-exclude=@perishlab/*"])
        for invalid in ("{bad", "123", "[1]", '{"a":1}'):
            with self.subTest(invalid=invalid), self.assertRaises(Refusal):
                node.exclusions(repository.root, self.runner(invalid))
        def failed(argv, cwd):
            raise subprocess.CalledProcessError(1, argv, stderr="boom")
        with self.assertRaisesRegex(Refusal, "boom"):
            node.exclusions(repository.root, failed)

    def observed(self, ambient):
        repository = Repository()
        calls = []
        def execute(argv, cwd, env):
            configs = [value for key, value in env.items() if key.upper() == "NPM_CONFIG_USERCONFIG"]
            calls.append((argv, env, [Path(config).read_text() for config in configs]))
        with mock.patch.dict(os.environ, ambient):
            for key in [key for key in os.environ if key.upper() == "NPM_CONFIG_USERCONFIG" and key not in ambient]:
                del os.environ[key]
            node.suite(node.Suite(repository.root, repository.root / "out"), self.runner(), execute)
        return calls

    def test_install_alone_reads_the_registries_and_tests_hold_no_token(self):
        for ambient in ({}, {"NPM_CONFIG_USERCONFIG": os.devnull}):
            with self.subTest(ambient):
                (_, installed, config), (_, tested, unconfigured) = self.observed(dict(ambient, **{node.READER: "r"}))
                self.assertEqual(installed[node.READER], "r")
                self.assertEqual(config, ["//npm.pkg.github.com/:_authToken=${WHARF_PACKAGES_TOKEN}\n"])
                self.assertNotIn(node.READER, tested)
                self.assertEqual(unconfigured, [])

    def test_the_caller_npm_configuration_never_reaches_installation_or_tests(self):
        canary = Path(self.enterContext(tempfile.TemporaryDirectory())) / "npmrc"
        canary.write_text("//npm.pkg.github.com/:_authToken=CANARY_ONLY_SAFE\n")
        for reader in ({}, {node.READER: "r"}):
            with self.subTest(reader):
                calls = self.observed(dict(reader, NPM_CONFIG_USERCONFIG=str(canary), npm_config_userconfig=str(canary)))
                for argv, env, configs in calls:
                    self.assertNotIn(str(canary), env.values(), argv)
                    self.assertFalse(any("CANARY_ONLY_SAFE" in config for config in configs), argv)

    def test_refuses_a_different_prepared_toolchain(self):
        repository = Repository()
        runner = lambda argv, cwd: {"node": "v22.0.0\n", "pnpm": "11.28.5\n"}[argv[0]]
        with self.assertRaises(Refusal):
            node.suite(node.Suite(repository.root, repository.root / "out"), runner, lambda *args: None)
