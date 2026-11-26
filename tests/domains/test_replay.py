import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.content.static import evidence
from lib.identity.guard import execution, material, replay, snapshot
from lib.refusal import Refusal
from tests.lib.cargo import test_replay as fixtures
from tests.lib.identity.test_guard import AUTHORITY, runtime
from tests.lib.media.repository import Repository as MediaRepository
from tests.lib.store.memory import Memory

DOMAIN = {"node.version": "24.18.0", "pnpm.version": "11.28.5", "rust.version": "1.96.1"}
CONTROLS = {"plumb": "plumb v0.87.0", "ectropy": "ectropy v0.20.0", "authority": AUTHORITY, "stable": {}}


def make_snapshot(identity, packages, head, tree):
    resolution = {"context": "ci-latest", "tree": tree, "packages": packages}
    guard = json.loads(runtime(commit=head, tree=tree, root="/verified/original", proof={"repository": identity["repository"], "resolution": resolution, "platform": "linux-x86_64"}))
    return {"schema": "wharf.release.snapshot/v1", "source": identity, "head": head, "tree": tree, "packages": packages, "controls": CONTROLS, "domain": DOMAIN, "guard": guard}


class Mixed(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.Replay()
        fixture.setUp()
        self.root = fixture.root
        self.repository = fixture.repository
        self.repository.write("pnpm-lock.yaml", "old npm inputs\n")
        self.repository.commit()
        self.repository.write("pnpm-lock.yaml", "planned npm inputs\n")
        fixtures.git(self.root, "add", "--update")
        self.tree = fixtures.git(self.root, "write-tree").strip()
        fixtures.git(self.root, "reset", "--hard", "HEAD")
        self.expected = dict(fixture.expected, tree=self.tree)
        self.expected["packages"].append({"ecosystem": "npm", "name": "@perishlab/demo", "version": "0.9.0"})
        request = SimpleNamespace(expected=self.expected, identity={"marker": "v1.0.0"}, domain={})
        self.held = {"root": self.root, "request": request, "tools": {name: {"path": name} for name in ("git", "cargo", "pnpm", "plumb")}, "env": {}, "inspect": self.inspect}
        self.calls = []

    def inspect(self, argv, root, env):
        if argv[0] == "git":
            return fixtures.git(root, *argv[1:])
        self.calls.append(argv)
        self.assertEqual(argv, ["plumb", "lift", str(root), "--json"])
        self.repository.write("pnpm-lock.yaml", "planned npm inputs\n")
        fixtures.git(root, "add", "--update")
        return json.dumps({"context": "lift", "tree": fixtures.git(root, "write-tree").strip(), "packages": self.expected["packages"]})

    def test_changed_mixed_inputs_use_released_lift_and_reconstruct_the_planned_tree(self):
        with mock.patch.object(snapshot.replay, "resolved", side_effect=AssertionError("incomplete replay")):
            result = snapshot.resolution(self.held)
        self.assertEqual(result, {"context": "lift", "tree": self.tree, "packages": self.expected["packages"]})
        self.assertEqual(len(self.calls), 1)

    def test_changed_resolution_still_refuses_the_exact_plan(self):
        self.expected["tree"] = "0" * 40
        result = snapshot.resolution(self.held)
        self.expected.update(schema="wharf.release.snapshot/v1", source=self.held["request"].identity, head="a" * 40, controls={}, domain={}, guard={})
        with self.assertRaisesRegex(Refusal, "differs from the verified plan.*tree"):
            snapshot.receipt(self.held, "a" * 40, result, {})

    def test_released_lift_failure_is_not_replaced_by_replay_or_a_pass(self):
        self.held["inspect"] = mock.Mock(side_effect=Refusal("latest moved"))
        with mock.patch.object(snapshot, "git", return_value="0" * 40), self.assertRaisesRegex(Refusal, "latest moved"):
            snapshot.resolution(self.held)


class MaterialReplay(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.Replay()
        fixture.setUp()
        self.root = fixture.root
        self.repository = fixture.repository
        self.repository.write("plumb.toml", '[release.cargo]\nregistry = "perish"\npackages = ["demo"]\n[release.npm]\npackages = ["@perishlab/owned"]\n')
        self.repository.write("pnpm-lock.yaml", "old npm\n")
        self.repository.edit("Cargo.lock", 'version = "0.9.0"', 'version = "0.8.0"')
        self.repository.edit("Cargo.lock", '"demo 0.9.0"', '"demo 0.8.0"')
        self.repository.commit()
        self.commit = fixtures.git(self.root, "rev-parse", "HEAD").strip()
        self.source_tree = fixtures.git(self.root, "rev-parse", "HEAD^{tree}").strip()
        self.identity = {"repository": "PerishLab/demo", "marker": "v1.0.0", "commit": self.commit, "tree": self.source_tree}
        self.packages = [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}, {"ecosystem": "npm", "name": "@perishlab/owned", "version": "0.9.0"}]
        self.lifted_lock = (self.root / "Cargo.lock").read_text().replace("0.8.0", "0.9.0")
        self.lifted_npm = "lifted npm\n"
        self.payload, self.expected = self.capture_plan(self.lifted_lock, self.lifted_npm)

    def capture_plan(self, lock_body, npm_body):
        with tempfile.TemporaryDirectory() as directory:
            env = snapshot.environment(Path(directory), DOMAIN)
            held = {"root": self.root, "tools": {"git": {"path": "git"}}, "env": env, "inspect": evidence.inspect}
            (self.root / "Cargo.lock").write_text(lock_body)
            (self.root / "pnpm-lock.yaml").write_text(npm_body)
            snapshot.git(held, "add", "--update")
            tree = snapshot.git(held, "write-tree")
            head = snapshot.committed(held, {"tree": tree})
            expected = make_snapshot(self.identity, self.packages, head, tree)
            payload = material.capture(self.root, expected)
            fixtures.git(self.root, "reset", "--hard", self.commit)
            return payload, expected

    def held(self, expected=None, payload=None, marker_override=None):
        target = self.expected if expected is None else expected
        identity = self.identity if marker_override is None else dict(self.identity, marker=marker_override)
        request = snapshot.Request(self.root, identity, DOMAIN, expected=target, material=material.restorer(self.payload if payload is None else payload))
        return {"root": self.root, "request": request, "tools": {name: {"path": name} for name in ("git", "cargo", "pnpm", "plumb")}, "env": {"USERPROFILE": str(self.root.parent)}, "inspect": self.inspect}

    def inspect(self, argv, root, env):
        if argv[0] == "git":
            return fixtures.git(root, *argv[1:])
        raise AssertionError(f"material replay must not invoke {argv}")

    def npm_reader(self, versions):
        return lambda name, location: {"name": name, "dist-tags": {"latest": versions[name]}, "versions": {versions[name]: {"name": name, "version": versions[name]}}}

    def test_mixed_lifted_snapshot_replays_after_own_cargo_and_npm_publish_without_lift(self):
        held = self.held()
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.8.0", "0.9.0", "1.0.0"])), mock.patch.object(replay, "metadata", side_effect=self.npm_reader({"@perishlab/owned": "1.0.0"})):
            result = snapshot.resolution(held)
        self.assertEqual(result, {"context": "lift", "tree": self.expected["tree"], "packages": self.packages})
        self.assertEqual((self.root / "Cargo.lock").read_text(), self.lifted_lock)
        self.assertEqual((self.root / "pnpm-lock.yaml").read_text(), self.lifted_npm)

    def test_foreign_drift_and_wrong_own_version_refuse_before_restoring_material(self):
        held = self.held()
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0", "1.1.0"])), self.assertRaisesRegex(Refusal, "demo moved to 1.1.0"):
            snapshot.resolution(held)
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0", "1.0.0"])), mock.patch.object(replay, "metadata", side_effect=self.npm_reader({"@perishlab/owned": "1.1.0"})), self.assertRaisesRegex(Refusal, "@perishlab/owned moved"):
            snapshot.resolution(held)
        rc_expected = dict(self.expected, source=dict(self.identity, marker="v1.0.0-rc.1"))
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0", "1.0.0"])), self.assertRaisesRegex(Refusal, "demo moved to 1.0.0"):
            snapshot.resolution(self.held(expected=rc_expected, marker_override="v1.0.0-rc.1"))

    def test_non_publishable_workspace_member_is_not_exempted(self):
        self.repository.write("plumb.toml", '[release.cargo]\nregistry = "perish"\npackages = ["demo-cli"]\n')
        self.repository.commit()
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0", "1.0.0"])), self.assertRaisesRegex(Refusal, "demo moved to 1.0.0"):
            replay.current(self.root, {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}]}, "1.0.0")
        self.repository.write("plumb.toml", '[release.cargo]\nregistry = "perish"\npackages = ["demo"]\n')
        self.repository.edit("crates/lib/Cargo.toml", "version.workspace = true", "version.workspace = true\npublish = false")
        self.repository.commit()
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0", "1.0.0"])), self.assertRaisesRegex(Refusal, "demo moved to 1.0.0"):
            replay.current(self.root, {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}]}, "1.0.0")

    def test_tampered_material_refuses_even_when_registries_are_current(self):
        tampered = copy.deepcopy(self.payload)
        tampered["files"][0]["content"] = "tampered\n"
        held = self.held(payload=tampered)
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0"])), mock.patch.object(replay, "metadata", side_effect=self.npm_reader({"@perishlab/owned": "0.9.0"})), self.assertRaisesRegex(Refusal, "complete tree"):
            snapshot.resolution(held)
        wrong_digest = dict(self.payload, snapshot="0" * 64)
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.9.0"])), mock.patch.object(replay, "metadata", side_effect=self.npm_reader({"@perishlab/owned": "0.9.0"})), self.assertRaisesRegex(Refusal, "another verified combination"):
            snapshot.resolution(self.held(payload=wrong_digest))

    def test_pure_npm_and_zero_package_repos_replay_with_material_without_cargo(self):
        for packages in ([{"ecosystem": "npm", "name": "@perishlab/owned", "version": "0.9.0"}], []):
            repo = MediaRepository({"plumb.toml": '[release.npm]\npackages = ["@perishlab/owned"]\n', "pnpm-lock.yaml": "old\n"})
            commit = fixtures.git(repo.root, "rev-parse", "HEAD").strip()
            identity = {"repository": "PerishLab/web", "marker": "v1.0.0", "commit": commit, "tree": fixtures.git(repo.root, "rev-parse", "HEAD^{tree}").strip()}
            with tempfile.TemporaryDirectory() as directory:
                env = snapshot.environment(Path(directory), DOMAIN)
                git_held = {"root": repo.root, "tools": {"git": {"path": "git"}}, "env": env, "inspect": evidence.inspect}
                (repo.root / "pnpm-lock.yaml").write_text("new\n")
                snapshot.git(git_held, "add", "--update")
                tree = snapshot.git(git_held, "write-tree")
                head = snapshot.committed(git_held, {"tree": tree})
                expected = make_snapshot(identity, packages, head, tree)
                payload = material.capture(repo.root, expected)
                fixtures.git(repo.root, "reset", "--hard", commit)
            request = snapshot.Request(repo.root, identity, DOMAIN, expected=expected, material=material.restorer(payload))
            held = {"root": repo.root, "request": request, "tools": {"git": {"path": "git"}, "pnpm": {"path": "pnpm"}}, "env": {}, "inspect": self.inspect}
            with mock.patch.object(replay.registry, "fetch", side_effect=AssertionError("no cargo fetch")), mock.patch.object(replay, "metadata", side_effect=self.npm_reader({"@perishlab/owned": "1.0.0"}) if packages else AssertionError("no npm fetch")):
                result = snapshot.resolution(held)
            self.assertEqual(result, {"context": "lift", "tree": expected["tree"], "packages": packages})
            self.assertEqual((repo.root / "pnpm-lock.yaml").read_text(), "new\n")

    def test_pre_material_fallback_pins_lifted_own_crate_before_dropping_foreign_entries(self):
        lock = self.root / "Cargo.lock"
        lock.write_text(lock.read_text() + fixtures.THIRD + f'\n[[package]]\nname = "plumb"\nversion = "0.103.6"\nsource = "{replay.SOURCE}"\n')
        self.repository.commit()
        expected = {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}, {"ecosystem": "cargo", "name": "plumb", "version": "0.103.7"}]}
        request = SimpleNamespace(expected=expected, identity={"marker": "v1.0.0"})
        held = {"root": self.root, "request": request, "tools": {"cargo": {"path": "cargo"}, "git": {"path": "git"}}}
        calls = []

        def fake_cargo(item, argv):
            calls.append(argv)
            text = lock.read_text()
            if argv[1] == "update":
                self.assertEqual(argv, ["cargo", "update", "--package", f"{replay.SOURCE}#demo@0.8.0", "--precise", "0.9.0"])
                lock.write_text(text.replace("0.8.0", "0.9.0"))
            else:
                self.assertEqual(argv, ["cargo", "metadata", "--format-version", "1"])
                self.assertIn(("demo", "0.9.0"), replay.locked(self.root))
                lock.write_text(text + f'\n[[package]]\nname = "plumb"\nversion = "0.103.7"\nsource = "{replay.SOURCE}"\n')

        run = lambda item, *args: fixtures.git(self.root, *args).strip()
        with mock.patch.object(replay.registry, "fetch", fixtures.index(demo=["0.8.0", "0.9.0", "1.0.0"], plumb=["0.103.7"])):
            result = replay.resolved(held, fake_cargo, run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["packages"], expected["packages"])
        self.assertIn(fixtures.THIRD, lock.read_text())

    def test_execution_propagates_material_restorer_to_perform_and_admit(self):
        seen = []
        bucket = Memory()
        held = {"repository": "PerishLab/demo", "marker": "v1.0.0", "wharf": "b" * 40, "run": "7", "attempt": "2", "planned": "1", "source": str(self.root)}
        document = {"context": dict(held, **self.identity, attempt="1", snapshot=self.expected, material=self.payload)}

        @contextmanager
        def prepared(request):
            seen.append(callable(request.material))
            yield self.root, self.expected, lambda: None

        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(execution.r2, "configured", return_value=bucket), mock.patch.object(execution.plan, "read", return_value=document), mock.patch.object(execution.toolchain, "versions", return_value=DOMAIN), mock.patch.object(snapshot, "prepared", prepared), mock.patch.object(execution.parameters, "answer"):
            os.environ.pop("WHARF_CONFIGURATION", None)
            execution.perform("cargo-publish", lambda scoped: scoped["publication"](), held)
        self.assertEqual(seen, [True, True])
