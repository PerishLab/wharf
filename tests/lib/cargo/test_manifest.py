import unittest

from lib.cargo import manifest, publish
from lib.refusal import Refusal
from tests.lib.cargo.test_basis import Repository


class Virtual(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def changed(self, old, new):
        self.repository.edit("Cargo.toml", old, new)
        self.repository.commit()
        return manifest.virtual(self.repository.root)

    def test_a_root_without_a_package_or_default_members_is_virtual(self):
        self.assertTrue(manifest.virtual(self.repository.root))

    def test_a_root_package_is_not_virtual(self):
        self.assertFalse(self.changed("[workspace]\n", '[package]\nname = "root"\n\n[workspace]\n'))

    def test_default_members_narrow_what_a_plain_cargo_test_runs(self):
        self.assertFalse(self.changed('members = ["crates/*"]\n', 'members = ["crates/*"]\ndefault-members = ["crates/cli"]\n'))


class Closure(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def target(self, kind, spec):
        path = "crates/cli/Cargo.toml"
        body = (self.repository.root / path).read_text()
        self.repository.write(path, body + f'\n[target.\'cfg(windows)\'.{kind}]\nalternate = {spec}\n')
        self.repository.commit()

    def test_target_paths_and_inherited_aliases_cover_all_dependency_kinds(self):
        for kind in manifest.PACKAGED:
            for inherited in [False, True]:
                self.repository = Repository()
                spec = '{ package = "other", path = "../other", version = "=0.0.0" }'
                if inherited:
                    root = (self.repository.root / "Cargo.toml").read_text()
                    self.repository.write("Cargo.toml", root + '\n[workspace.dependencies]\nalternate = { package = "other", path = "crates/other", version = "=0.0.0" }\n')
                    spec = "{ workspace = true }"
                self.target(kind, spec)
                workspace = manifest.members(self.repository.root, manifest.read(self.repository.root, "Cargo.toml"))
                actual = manifest.closure(self.repository.root, workspace, "demo-cli")
                expected = ["demo", "demo-cli"] if kind == "dev-dependencies" else ["demo", "demo-cli", "other"]
                self.assertEqual(actual, expected)
                self.assertEqual(manifest.closure(self.repository.root, workspace, "demo-cli", manifest.PACKAGED), ["demo", "demo-cli", "other"])

    def declared(self):
        self.repository.write("plumb.toml", '[release.cargo]\nregistry="perish"\npackages=["demo-cli","other","demo"]\n')
        self.repository.commit()

    def test_target_dependency_precedes_its_consumer_independent_of_list_order(self):
        self.target("dependencies", '{ package = "other", path = "../other", version = "=0.0.0", optional = true }')
        self.declared()
        self.assertEqual(publish.publishable(self.repository.root), [("demo", "perish"), ("other", "perish"), ("demo-cli", "perish")])

    def test_target_cycle_refuses_publication(self):
        for kind in manifest.PACKAGED:
            self.repository = Repository()
            self.repository.write("crates/lib/Cargo.toml", f'[package]\nname="demo"\nversion.workspace=true\n[target.\'cfg(windows)\'.{kind}]\npeer={{package="other",path="../other",version="=0.0.0"}}\n')
            body = (self.repository.root / "crates/other/Cargo.toml").read_text()
            self.repository.write("crates/other/Cargo.toml", body + '\ndemo = { path = "../lib", version = "=0.0.0" }\n')
            self.declared()
            with self.assertRaisesRegex(Refusal, "cycle"):
                publish.publishable(self.repository.root)

    def test_target_path_outside_members_refuses(self):
        self.target("build-dependencies", '{ path = "../../outside", version = "=0.0.0" }')
        self.declared()
        with self.assertRaisesRegex(Refusal, "outside the workspace"):
            publish.publishable(self.repository.root)
