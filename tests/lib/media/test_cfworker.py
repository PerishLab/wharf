import os
import ssl
import unittest
import urllib.error
from unittest import mock

from lib.media import cfworker
from lib.refusal import Refusal
from tests.lib.media.repository import Repository

FILES = {
    "apps/web/package.json": '{"name": "@demo/web", "private": true}\n',
    "apps/web/wrangler.jsonc": '{\n\t// comment\n\t"name": "demo",\n\t"account_id": "acc",\n\t"routes": [{ "pattern": "demo.example", "custom_domain": true }]\n}\n',
}


class Worker(unittest.TestCase):
    def setUp(self):
        self.repository = Repository(FILES)
        self.calls = []
        self.envs = []

    def runner(self, argv, cwd, env=None):
        self.calls.append(argv)
        self.envs.append(env)
        return {"node": "v24.18.0\n", "pnpm": "11.13.0\n"}.get(argv[0], "")

    def test_lists_workers_from_native_manifests(self):
        self.assertEqual(cfworker.workers(self.repository.root), [{"name": "demo", "directory": "apps/web", "package": "@demo/web", "domains": ["demo.example"]}])

    def test_builds_deploys_and_reaches_the_domain(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t"}):
            receipt = cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "out"), self.runner, lambda url: 200)
        self.assertEqual(receipt["workers"], [{"name": "demo", "domains": {"demo.example": 200}}])
        self.assertIn(["pnpm", "exec", "wrangler", "deploy"], self.calls)

    def test_install_reads_the_registries_with_the_read_token_alone(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t", "WHARF_PACKAGES_TOKEN": "r"}):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "out"), self.runner, lambda url: 200)
        installed = self.envs[self.calls.index(["pnpm", "install", "--frozen-lockfile"])]
        self.assertIn("NPM_CONFIG_USERCONFIG", installed)
        self.assertTrue(all(env is None for argv, env in zip(self.calls, self.envs) if argv[:2] != ["pnpm", "install"] and argv[0] == "pnpm"))

    def test_refuses_without_a_token_or_when_unreachable(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: ""}), self.assertRaises(Refusal):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "a"), self.runner, lambda url: 200)
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t"}), self.assertRaises(Refusal):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "b"), self.runner, lambda url: 503)

    def deployed(self, probe, sleeps):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t"}):
            return cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "out"), self.runner, probe, sleeps.append)

    def test_waits_for_a_new_domain_to_resolve_and_serve_tls(self):
        failures = iter([urllib.error.URLError("no address"), ssl.SSLError("handshake")])

        def probe(url):
            failure = next(failures, None)
            if failure is not None:
                raise failure
            return 200

        sleeps = []
        receipt = self.deployed(probe, sleeps)
        self.assertEqual(receipt["workers"], [{"name": "demo", "domains": {"demo.example": 200}}])
        self.assertEqual(sleeps, [cfworker.PAUSE, cfworker.PAUSE])

    def test_refuses_a_domain_that_never_becomes_reachable(self):
        def probe(url):
            raise urllib.error.URLError("no address")

        sleeps = []
        with self.assertRaisesRegex(Refusal, "stayed unreachable"):
            self.deployed(probe, sleeps)
        self.assertEqual(len(sleeps), cfworker.PATIENCE - 1)

    def test_refuses_a_status_other_than_200_without_waiting(self):
        sleeps = []
        with self.assertRaisesRegex(Refusal, "answered"):
            self.deployed(lambda url: 404, sleeps)
        self.assertEqual(sleeps, [])

    def test_answer_reports_an_http_error_status_instead_of_raising(self):
        error = urllib.error.HTTPError("https://demo.example/", 503, "unavailable", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(cfworker.answer("https://demo.example/"), 503)
