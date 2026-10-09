import json
import os
import unittest
from unittest import mock

from lib.media import cfworker
from lib.refusal import Refusal
from tests.lib.media import test_cfworker
from tests.lib.preview.test_lane import declared


class LaneRelease(unittest.TestCase):
    def setUp(self):
        self.fixture = test_cfworker.Worker()
        self.fixture.setUp()
        self.fixture.review()
        self.repository = self.fixture.repository

    def refusal(self):
        with mock.patch.dict(os.environ, {cfworker.TOKEN: "fixture"}), self.assertRaises(Refusal):
            cfworker.deploy(cfworker.Deploy(self.repository.root, self.repository.root / "out"), self.fixture.runner)
        self.assertEqual(self.fixture.calls, [])

    def test_mapping_mismatch_refuses_before_any_product_command(self):
        for key, value in (("name", "other"), ("account_id", "b" * 32)):
            path = self.repository.root / "apps/review/wrangler.jsonc"
            original = json.loads(path.read_text())
            self.repository.write("apps/review/wrangler.jsonc", json.dumps(dict(original, **{key: value})))
            with self.subTest(key=key):
                self.refusal()
            self.repository.write("apps/review/wrangler.jsonc", json.dumps(original))

    def test_multiple_bindings_share_one_parent_without_releasing_it(self):
        body = declared()
        binding = body[body.index("[lane.app.review.binding.preview]"):].replace("binding.preview", "binding.'preview.a'")
        self.repository.write("plumb.toml", body + binding)
        workers = cfworker.workers(self.repository.root)
        self.assertEqual([worker["name"] for worker in workers], ["demo"])

    def test_app_identity_is_not_package_path_or_resource(self):
        self.repository.write("plumb.toml", declared(app="design"))
        self.assertEqual(len(cfworker.workers(self.repository.root)), 1)

    def test_unknown_ambiguous_and_retired_bindings_refuse_before_commands(self):
        bodies = [
            '[preview.app.review]\npath="apps/review"\n',
            declared().replace("binding.preview", "binding.preview.a"),
            declared().replace('["inspect", "deploy"]', '["inspect", "deploy", "publish"]'),
            declared().replace('publication = "wharf"', 'publication = "wharf.a"'),
            declared().replace('resource = "review"', 'resource = "review"\ntoken = "private"'),
        ]
        for body in bodies:
            self.repository.write("plumb.toml", body)
            with self.subTest(body=body):
                self.refusal()

    def test_declaration_and_package_must_be_tracked_regular_files(self):
        for name in ("plumb.toml", "apps/review/package.json"):
            self.repository.git("rm", "--cached", name)
            with self.subTest(name=name):
                self.refusal()
            self.repository.git("add", name)
        package = self.repository.root / "apps/review/package.json"
        package.unlink()
        package.symlink_to("../web/package.json")
        self.refusal()

    def test_duplicate_worker_or_package_fields_refuse(self):
        for name in ("apps/review/wrangler.jsonc", "apps/review/package.json"):
            path = self.repository.root / name
            original = path.read_text()
            field = '"name":"review",' if "wrangler" in name else '"name":"@demo/review",'
            self.repository.write(name, "{" + field + original.lstrip()[1:])
            with self.subTest(name=name):
                self.refusal()
            self.repository.write(name, original)
