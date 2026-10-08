import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.check import dependency
from lib.check.locking import checkout, objects
from lib.content.static import workspace
from lib.refusal import Refusal

LOCK = b"lockfileVersion: '9.0'\nsettings:\n  autoInstallPeers: true\n  excludeLinksFromLockfile: false\nimporters:\n  .: {}\n  packages/one: {}\n  packages/two: {}\npackages: {}\nsnapshots: {}\n"


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.env = dict(workspace.environment(self.temporary.name), GIT_TEMPLATE_DIR="")
        self.git("init", "--quiet")
        self.write("package.json", json.dumps({"scripts": {"preinstall": "SECRET"}}))
        self.write("pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
        self.write("pnpm-lock.yaml", LOCK.decode())
        self.write("packages/one/package.json", '{"name":"one"}')
        self.write("packages/two/package.json", '{"name":"two"}')
        self.write(".pnpmfile.cjs", "throw Error('SECRET');\n")
        self.git("remote", "add", "origin", "https://github.com/PerishLab/crest.git")
        self.commit()

    def git(self, *arguments):
        result = subprocess.run(["/usr/bin/git", "-C", str(self.root), *arguments], env=self.env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "fixture")
        self.expected = {"repository": "PerishLab/crest", "commit": self.git("rev-parse", "HEAD"), "tree": self.git("rev-parse", "HEAD^{tree}")}

    def test_complete_projection_and_exact_source_without_execution(self):
        result = checkout.collect(self.root, self.expected)
        self.assertEqual(result["source"], self.expected)
        self.assertEqual(set(result["digests"]), {"package.json", "packages/one/package.json", "packages/two/package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml"})
        self.assertEqual(result["files"]["pnpm-lock.yaml"].encode(), LOCK)
        self.assertEqual(result["digests"]["pnpm-lock.yaml"], hashlib.sha256(LOCK).hexdigest())
        self.assertEqual(set(result["files"]), {".npmrc", "pnpm-workspace.yaml", "pnpm-lock.yaml"})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_selected_manifest_cannot_be_omitted_by_caller(self):
        self.write("packages/three/package.json", '{"name":"three"}')
        self.commit()
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)

    def test_missing_required_blob_and_selected_symlink_refuse(self):
        path = self.root / "packages/one/package.json"
        path.unlink()
        path.symlink_to("../../package.json")
        self.commit()
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)
        path.unlink()
        self.write("packages/one/package.json", '{"name":"one"}')
        (self.root / "pnpm-workspace.yaml").unlink()
        self.commit()
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)

    def test_dirty_ignored_and_foreign_identity_refuse(self):
        for changed in [{"commit": "a" * 40}, {"tree": "b" * 40}, {"repository": "PerishLab/other"}, {"root": str(self.root)}]:
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                checkout.collect(self.root, dict(self.expected, **changed))
        self.write(".gitignore", "ignored/\n")
        self.commit()
        self.write("ignored/package.json", "SECRET")
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)

    def test_source_mutation_during_collection_refuses(self):
        original = objects.blob

        def changed(root, entry, env, limit):
            body = original(root, entry, env, limit)
            self.write("changed", "SECRET")
            return body

        with mock.patch.object(objects, "blob", side_effect=changed), self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)

    def test_changed_readback_and_corrupt_blob_refuse(self):
        before = workspace.acquired(self.root, self.expected)
        with mock.patch.object(workspace, "acquired", side_effect=[before, dict(before, tree="f" * 40)]), self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)
        identity = self.git("rev-parse", "HEAD:package.json")
        with mock.patch.object(objects, "command", return_value=b"SECRET"), self.assertRaises(Refusal) as caught:
            objects.blob(self.root, ("100644", identity), self.env, dependency.LIMIT)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_host_environment_and_arguments_are_not_inherited(self):
        seen = []
        original = objects.command

        def observed(root, arguments, env, limit):
            seen.append((arguments, env))
            return original(root, arguments, env, limit)

        with mock.patch.dict(os.environ, GH_TOKEN="SECRET", NODE_OPTIONS="SECRET", GIT_CONFIG_COUNT="99"), mock.patch.object(objects, "command", side_effect=observed):
            checkout.collect(self.root, self.expected)
        self.assertTrue(seen)
        self.assertTrue(all(not {"GH_TOKEN", "NODE_OPTIONS"}.intersection(env) for _, env in seen))
        self.assertTrue(all(arguments[0] in {"ls-tree", "cat-file"} for arguments, _ in seen))

    def test_unsupported_recursive_hidden_and_reserved_selection(self):
        for inventory, selectors in [({}, ["packages/**"]), ({"packages/.hidden/package.json": ()}, ["packages/*"]), ({"packages/node_modules/package.json": ()}, ["packages/*"]), ({"packages/../package.json": ()}, ["packages/*"])]:
            with self.subTest(selectors=selectors, inventory=inventory), self.assertRaises(Refusal):
                checkout.selected(inventory, selectors)
        self.assertEqual(checkout.selected({"examples/demo/package.json": ()}, ["packages/*"]), ["package.json"])

    def test_selected_file_and_aggregate_bounds(self):
        inventory = {f"packages/p{index}/package.json": () for index in range(dependency.FILES)}
        with self.assertRaises(Refusal):
            checkout.selected(inventory, ["packages/*"])
        self.write("packages/one/package.json", " " * dependency.LIMIT)
        self.commit()
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)

    def test_inventory_nul_schema_duplicates_and_bounds(self):
        entry = b"100644 blob " + b"a" * 40 + b"\tpackage.json\0"
        self.assertEqual(objects.inventory(entry), {"package.json": ("100644", "a" * 40)})
        for body in [entry[:-1], entry + entry, entry.replace(b"100644", b"160000"), entry.replace(b"blob", b"tree"), b"SECRET\0", b"x" * (objects.INVENTORY + 1)]:
            with self.subTest(size=len(body)), self.assertRaises(Refusal) as caught:
                objects.inventory(body)
            self.assertNotIn("SECRET", str(caught.exception))
        with mock.patch.object(objects, "ENTRIES", 1), self.assertRaises(Refusal):
            objects.inventory(entry + entry.replace(b"package.json", b"other.json"))

    def test_git_failure_output_and_timeout_bounds(self):
        for arguments, limit in [(["cat-file", "blob", "f" * 40], 10), (["ls-tree", "-r", "-z", self.expected["tree"]], 1)]:
            with self.subTest(arguments=arguments), self.assertRaises(Refusal) as caught:
                objects.command(self.root, arguments, self.env, limit)
            self.assertNotIn("SECRET", str(caught.exception))
        with mock.patch.object(objects, "TIMEOUT", 0), self.assertRaises(Refusal):
            objects.command(self.root, ["rev-parse", "HEAD"], self.env, 1024)

    def test_existing_acquisition_hooks_and_shared_metadata_refuse(self):
        (self.root / ".git/hooks").mkdir()
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)
        (self.root / ".git/hooks").rmdir()
        (self.root / ".git/objects/info/alternates").write_text("SECRET")
        with self.assertRaises(Refusal):
            checkout.collect(self.root, self.expected)
