import os
import unittest
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

    def runner(self, argv, cwd):
        self.calls.append(argv)
        return {"node": "v24.18.0\n", "pnpm": "11.13.0\n"}.get(argv[0], "")

    def test_lists_workers_from_native_manifests(self):
        self.assertEqual(cfworker.workers(self.repository.root), [{"name": "demo", "directory": "apps/web", "package": "@demo/web", "domains": ["demo.example"]}])

    def test_builds_deploys_and_reaches_the_domain(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t"}):
            receipt = cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "out"), self.runner, lambda url: 200)
        self.assertEqual(receipt["workers"], [{"name": "demo", "domains": {"demo.example": 200}}])
        self.assertIn(["pnpm", "exec", "wrangler", "deploy"], self.calls)

    def test_refuses_without_a_token_or_when_unreachable(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: ""}), self.assertRaises(Refusal):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "a"), self.runner, lambda url: 200)
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "t"}), self.assertRaises(Refusal):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "b"), self.runner, lambda url: 503)
