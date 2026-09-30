import json
import os
import tempfile
import unittest
from pathlib import Path

from lib.content import canonical
from lib.content.static import evidence, source
from lib.process import git
from lib.refusal import Refusal
from tests.lib.content.test_preview import intent, target
from tests.lib.media.repository import Repository

WORKER = {
    "name": "crest-review", "account_id": "a" * 32, "compatibility_date": "2026-09-30", "workers_dev": False,
    "assets": {"directory": "./dist", "not_found_handling": "404-page"}, "previews": {},
}
PACKAGE = {"name": "crest-review", "private": True, "scripts": {"build": "node build.mjs"}}


class StaticRepository(Repository):
    def __init__(self):
        super().__init__({
            "plumb.toml": '[preview.app.crest-review]\npath="apps/review"\npackage="crest-review"\nprovider="cfworker"\naccess="public"\n',
            "apps/review/package.json": json.dumps(PACKAGE), "apps/review/wrangler.jsonc": json.dumps(WORKER),
            "apps/review/build.mjs": "export {};\n", ".gitignore": "dist/\nnode_modules/\n",
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n", ".npmrc": "@perishlab:registry=https://npm.pkg.github.com/\n",
        })
        self.git("remote", "add", "origin", "https://github.com/PerishLab/crest.git")

    def request(self):
        env = evidence.clean(os.environ, tempfile.gettempdir())
        tracked = set(git(self.root, "ls-files").splitlines())
        config = source.configuration(self.root, "crest-review", tracked)
        return dict(intent(), source={"commit": git(self.root, "rev-parse", "HEAD"), "tree": git(self.root, "rev-parse", "HEAD^{tree}"), "declaration": config["digest"]})


def guarded(request, root):
    return {
        "schema": "plumb.guard-runtime/v1", "root": str(root), "ok": True, "strength": "full", "boundary": "head",
        "commit": request["source"]["commit"], "digest": "1" * 64,
        "guard": {"schema": "plumb.guard-proof/v1", "repository": request["repository"], "tree": request["source"]["tree"], "plumb": "v0.66.0@" + "2" * 40, "depot": "3" * 64, "platform": "linux", "digest": "4" * 64, "actions": [{"name": "shape", "input": "5" * 64, "world": "6" * 64}]},
    }


class Qualification(unittest.TestCase):
    def setUp(self):
        self.repository = StaticRepository()
        self.request = self.repository.request()
        self.env = evidence.clean(os.environ, tempfile.gettempdir())

    def test_exact_source_and_target(self):
        config = source.qualify(self.repository.root, self.request, target(), self.env)
        self.assertEqual(config["directory"], "apps/review/dist")
        self.assertEqual(config["digest"], self.request["source"]["declaration"])

    def test_dirty_untracked_and_changed_head_refuse(self):
        self.repository.write("untracked.html", "content")
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)
        self.repository.commit()
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)

    def test_wrong_request_identity(self):
        for key, value in (("commit", "2" * 40), ("tree", "3" * 40), ("declaration", "4" * 64)):
            request = dict(self.request, source=dict(self.request["source"], **{key: value}))
            with self.subTest(key=key), self.assertRaises(Refusal):
                source.qualify(self.repository.root, request, target(), self.env)

    def test_origin_mismatch(self):
        self.repository.git("remote", "set-url", "origin", "https://example.com/PerishLab/crest.git")
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)

    def test_target_mismatch(self):
        changed = dict(target(), worker="production")
        request = dict(self.request, registration=canonical.digest(changed))
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, request, changed, self.env)

    def test_worker_runtime_and_unknown_configuration_refuse(self):
        for key, value in (("main", "worker.js"), ("routes", []), ("build", {}), ("workers_dev", True), ("previews", {"vars": {}})):
            with self.subTest(key=key), self.assertRaises(Refusal):
                source.worker(json.dumps(dict(WORKER, **{key: value})).encode())

    def test_unsafe_assets_paths(self):
        for value in ("../private", "/tmp/output", ".", "dist//x", "dist/../x", "dist\\x"):
            with self.subTest(value=value), self.assertRaises(Refusal):
                source.directory(value)

    def test_duplicate_worker_identity_refuses(self):
        self.repository.write("apps/other/wrangler.jsonc", json.dumps(WORKER))
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_manifest_and_package_constraints(self):
        for package in (dict(PACKAGE, private=False), dict(PACKAGE, name="other"), dict(PACKAGE, scripts={"build": "node x", "prebuild": "node y"})):
            self.repository.write("apps/review/package.json", json.dumps(package))
            self.repository.commit()
            with self.subTest(package=package), self.assertRaises(Refusal):
                self.repository.request()

    def test_source_symlink_refuses(self):
        path = self.repository.root / "apps/review/package.json"
        path.unlink()
        path.symlink_to("../../../package.json")
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_fresh_checkout_and_output_ancestor(self):
        self.repository.write("node_modules/payload", "unselected")
        with self.assertRaises(Refusal):
            source.fresh(self.repository.root, self.env)
        path = self.repository.root / "apps/review/dist"
        path.symlink_to(tempfile.gettempdir(), target_is_directory=True)
        with self.assertRaises(Refusal):
            source.output(self.repository.root, "apps/review/dist/nested")

    def test_install_config_cannot_redirect_credentials(self):
        self.repository.write(".npmrc", "//example.com/:_authToken=${WHARF_PACKAGES_TOKEN}\n")
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_package_filter_cannot_select_multiple_apps(self):
        self.repository.write("apps/other/package.json", json.dumps(PACKAGE))
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()


class GuardEvidence(unittest.TestCase):
    def test_full_head_exact_binding(self):
        request = intent()
        proof = guarded(request, Path("/tmp/source"))
        self.assertEqual(evidence.guard(json.dumps(proof), request, "plumb v0.66.0"), proof)
        for key, value in (("ok", False), ("strength", "declared"), ("boundary", "staged"), ("commit", "0" * 40), ("schema", "unknown")):
            with self.subTest(key=key), self.assertRaises(Refusal):
                evidence.guard(json.dumps(dict(proof, **{key: value})), request, "plumb v0.66.0")
        with self.assertRaises(Refusal):
            evidence.guard(json.dumps(proof), request, "plumb v0.65.0")

    def test_json_duplicates_and_bounds_refuse(self):
        for body in ('{"a":1,"a":2}', 'x', ' ' * (evidence.LIMIT + 1)):
            with self.subTest(body=body[:20]), self.assertRaises(Refusal):
                evidence.decode(body)


if __name__ == "__main__":
    unittest.main()
