import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lib.content.static import evidence
from lib.identity.guard import execution, snapshot
from lib.media import oci
from lib.process import git
from lib.refusal import Refusal
from lib.store import plan
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory


class Execution(unittest.TestCase):
    def setUp(self):
        self.held = {"repository": "PerishLab/example", "marker": "v1.0.0", "wharf": "b" * 40, "run": "7", "planned": "1", "source": "/original"}
        self.context = dict(self.held, commit="a" * 40, tree="c" * 40, snapshot={"verified": "receipt"})

    def test_build_refuses_an_old_plan_before_running_source(self):
        del self.context["snapshot"]
        handler = mock.Mock()
        with mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value={"context": self.context}):
            with self.assertRaisesRegex(Refusal, "verified release snapshot"):
                execution.perform("binary", handler, self.held)
        handler.assert_not_called()

    def test_latest_movement_refuses_before_the_build(self):
        handler = mock.Mock()
        with mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value={"context": self.context}), mock.patch.object(execution.toolchain, "versions", return_value={}):
            with mock.patch.object(snapshot, "prepared", side_effect=Refusal("latest moved")), self.assertRaisesRegex(Refusal, "latest moved"):
                execution.perform("binary", handler, self.held)
        handler.assert_not_called()

    def test_build_uses_the_verified_checkout_and_confirms_before_writing(self):
        events = []
        def confirm():
            events.append("confirmed")
        @contextmanager
        def prepared(request):
            self.assertEqual(request.expected, self.context["snapshot"])
            self.assertEqual(request.source, Path("/original"))
            yield Path("/verified"), request.expected, confirm
        def build(held):
            self.assertEqual(held["source"], Path("/verified"))
            return execution.publish(held, "bucket", "key", "bytes")
        def publish(*args):
            events.append("written")
            return args
        with mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value={"context": self.context}), mock.patch.object(execution.toolchain, "versions", return_value={}):
            with mock.patch.object(snapshot, "prepared", prepared), mock.patch.object(execution.workload, "publish", publish):
                execution.perform("binary", build, self.held)
        self.assertEqual(events, ["confirmed", "written"])
        self.assertEqual(self.held["source"], "/original")

    def test_changed_content_writes_no_workload(self):
        def confirm():
            raise Refusal("tracked content changed")
        with mock.patch.object(execution.workload, "publish") as publish:
            with self.assertRaisesRegex(Refusal, "tracked content changed"):
                execution.publish({"confirm": confirm}, "bucket", "key", "bytes")
        publish.assert_not_called()

    def test_artifact_only_actions_need_no_new_resolution(self):
        handler = mock.Mock(return_value="bound")
        with mock.patch.object(snapshot, "prepared") as prepared:
            self.assertEqual(execution.perform("bind", handler, self.held), "bound")
        prepared.assert_not_called()


class Publication(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.answers = []
        self.held = {"repository": "PerishLab/example", "marker": "v1.0.0", "wharf": "b" * 40, "run": "7", "planned": "1", "source": "/original"}
        identity = dict(repository=self.held["repository"], marker=self.held["marker"], commit="a" * 40, tree="c" * 40)
        self.expected = dict(schema="wharf.release.snapshot/v1", source=identity, head="d" * 40, tree="e" * 40, packages=[], controls={}, domain={}, guard={})
        self.document = {"context": dict(self.held, **identity, attempt="1", snapshot=self.expected)}

    def perform(self, handler, final=None):
        @contextmanager
        def prepared(request):
            yield Path("/verified"), self.expected, lambda: None
            if final:
                final()
        with mock.patch.object(execution.r2, "configured", return_value=self.bucket), mock.patch.object(execution.plan, "read", return_value=self.document), mock.patch.object(execution.toolchain, "versions", return_value={}):
            with mock.patch.object(snapshot, "prepared", prepared), mock.patch.object(execution.parameters, "answer", side_effect=self.answers.append):
                return execution.perform("oci-publish", handler, self.held)

    def test_existing_binary_image_reports_only_its_reserved_combination(self):
        plan.reserve(self.bucket, self.document)
        runner = mock.Mock()
        def handler(held):
            return oci.publish(oci.Image(held["source"], Path("/binary"), "example", "registry/example:1.0.0", "v1.0.0"), runner)
        with mock.patch.object(oci, "exists", return_value=True):
            result = self.perform(handler)
        runner.assert_not_called()
        self.assertEqual(result["state"], "already-published")
        self.assertEqual(json.loads(self.answers[0]["snapshot"]), plan.combination(self.expected))

    def test_existing_publication_without_reservation_creates_no_evidence(self):
        runner = mock.Mock()
        def handler(held):
            return oci.publish(oci.Image(held["source"], Path("/binary"), "example", "registry/example:1.0.0", "v1.0.0"), runner)
        with mock.patch.object(oci, "exists", return_value=True), self.assertRaisesRegex(Refusal, "no verified reserved"):
            self.perform(handler)
        self.assertEqual((self.bucket.writes, self.answers), ([], []))

    def test_a_different_reserved_combination_is_not_attributed_to_this_job(self):
        plan.reserve(self.bucket, self.document)
        key = plan.publication(self.document["context"])
        self.bucket.objects[key] = b"different"
        with self.assertRaisesRegex(Refusal, "another verified combination"):
            self.perform(lambda held: {"state": "already-published"})
        self.assertEqual(self.answers, [])

    def test_admission_does_not_report_success_before_the_handler_finishes(self):
        def handler(held):
            held["publication"]()
            self.assertEqual(self.answers, [])
            return {"state": "published"}
        self.assertEqual(self.perform(handler), {"state": "published"})
        self.assertEqual(len(self.answers), 1)

    def test_handler_failure_after_admission_reports_no_combination(self):
        def handler(held):
            held["publication"]()
            raise Refusal("push failed")
        with self.assertRaisesRegex(Refusal, "push failed"):
            self.perform(handler)
        self.assertEqual(self.answers, [])

    def test_final_snapshot_failure_reports_no_combination(self):
        plan.reserve(self.bucket, self.document)
        def failed():
            raise Refusal("source changed at exit")
        with self.assertRaisesRegex(Refusal, "source changed at exit"):
            self.perform(lambda held: {"state": "already-published"}, failed)
        self.assertEqual(self.answers, [])


class Content(unittest.TestCase):
    def setUp(self):
        self.repository = Repository({"package.json": "{}\n"})
        self.held = {"root": self.repository.root, "tools": {"git": {"path": "git"}}, "env": None, "inspect": self.inspect}
        self.head = git(self.repository.root, "rev-parse", "HEAD")
        self.tree = git(self.repository.root, "rev-parse", "HEAD^{tree}")

    def inspect(self, argv, cwd, env):
        return git(cwd, *argv[1:])

    def test_source_mutation_refuses_with_the_original_head_preserved(self):
        self.repository.write("package.json", '{"changed":true}\n')
        with self.assertRaisesRegex(Refusal, "tracked content"):
            snapshot.unchanged(self.held, self.head, self.tree)
        self.assertEqual(git(self.repository.root, "rev-parse", "HEAD"), self.head)

    def test_head_movement_refuses_even_when_the_tree_matches(self):
        with self.assertRaisesRegex(Refusal, "identity changed"):
            snapshot.unchanged(self.held, "d" * 40, self.tree)

    def test_latest_or_control_movement_invalidates_the_saved_plan(self):
        identity = {"repository": "PerishLab/example", "commit": self.head, "tree": self.tree, "marker": "v1.0.0"}
        expected = {"schema": "wharf.release.snapshot/v1", "source": identity, "head": self.head, "tree": self.tree, "packages": [], "controls": {}, "domain": {}, "guard": {}}
        held = {"request": snapshot.Request(self.repository.root, identity, {}, expected)}
        for result, world in (({"tree": self.tree, "packages": [{"ecosystem": "cargo", "name": "plumb", "version": "changed"}]}, {}), ({"tree": self.tree, "packages": []}, {"plumb": "changed"})):
            with self.subTest(world=world), self.assertRaisesRegex(Refusal, "fresh plan"):
                snapshot.receipt(held, self.head, result, world)


class Checkout(unittest.TestCase):
    def setUp(self):
        self.repository = Repository({"file": "source"})
        self.root = self.repository.root / "disposable"

    def test_owned_destination_is_cleaned_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, "build failed"):
            with snapshot.checkout(self.repository.root.parent / "source", self.root):
                (self.root / "output").write_text("compiled")
                raise RuntimeError("build failed")
        self.assertFalse(self.root.exists())

    def test_existing_destination_and_source_are_preserved(self):
        for root in (self.repository.root, self.repository.root.parent):
            with self.subTest(root=root), self.assertRaises(Refusal):
                with snapshot.checkout(self.repository.root, root):
                    self.fail("must refuse foreign source")
        self.assertEqual((self.repository.root / "file").read_text(), "source")

    def test_symlink_destination_refuses_without_touching_target(self):
        self.root.symlink_to(self.repository.root, target_is_directory=True)
        with self.assertRaises(Refusal):
            with snapshot.checkout(self.repository.root, self.root):
                self.fail("must refuse aliases")
        self.assertTrue(self.root.is_symlink())


class Environment(unittest.TestCase):
    def test_release_and_preview_keep_their_distinct_execution_contracts(self):
        ambient = {"PATH": "/trusted/bin", "NODE_OPTIONS": "--require /product/hook", "NPM_CONFIG_REGISTRY": "https://untrusted.invalid", "GIT_CONFIG_COUNT": "1", "PROVIDER_TOKEN": "excluded"}
        domain = {"rust.version": "1.96.1"}
        with mock.patch.dict(os.environ, ambient, clear=True), tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            static = evidence.clean(os.environ, home)
            release = snapshot.environment(home, domain)
            for name in ("NPM_CONFIG_IGNORE_SCRIPTS", "NPM_CONFIG_IGNORE_PNPMFILE"):
                self.assertEqual(static[name], "true")
                self.assertNotIn(name, release)
            for name in ("NODE_OPTIONS", "NPM_CONFIG_REGISTRY", "GIT_CONFIG_COUNT", "PROVIDER_TOKEN"):
                self.assertNotIn(name, release)
            self.assertEqual(release["HOME"], temporary)
            self.assertEqual(release["PATH"], ambient["PATH"])
            self.assertEqual(release["CI"], "true")
            self.assertEqual(release["RUSTUP_TOOLCHAIN"], domain["rust.version"])
            self.assertEqual(release["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(release["GIT_AUTHOR_DATE"], "@0 +0000")

    def test_package_reader_uses_only_the_owned_private_configuration(self):
        ambient = {"PATH": "/trusted/bin", snapshot.READER: "test-reader", "NPM_CONFIG_USERCONFIG": "/untrusted/npmrc"}
        with mock.patch.dict(os.environ, ambient, clear=True), tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            release = snapshot.environment(home, {"rust.version": "1.96.1"})
            config = Path(release["NPM_CONFIG_USERCONFIG"])
            self.assertEqual(config, home / "npmrc")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn("/:_authToken=" + ambient[snapshot.READER], config.read_text())
            self.assertNotIn("${", config.read_text())
            self.assertNotIn(snapshot.READER, release)
            self.assertEqual({name for name in release if name.startswith("NPM_CONFIG_")}, {"NPM_CONFIG_USERCONFIG"})

    def test_reader_cannot_inject_another_configuration_binding(self):
        with mock.patch.dict(os.environ, {snapshot.READER: "test-reader\nignore-scripts=false"}, clear=True), tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with self.assertRaisesRegex(Refusal, "one configuration line"):
                snapshot.environment(home, {"rust.version": "1.96.1"})
            self.assertFalse((home / "npmrc").exists())


class System(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and Path("/usr/sbin/sshd").is_file(), "system SSH daemon unavailable")
    def test_snapshot_finds_prepared_system_daemon_after_tool_path_reconstruction(self):
        env = {"PATH": "/usr/bin:/bin"}
        self.assertIsNone(shutil.which("sshd", path=env["PATH"]))
        snapshot.system({"env": env})
        self.assertEqual(Path(shutil.which("sshd", path=env["PATH"])).resolve(), Path("/usr/sbin/sshd").resolve())
        self.assertEqual(set(env), {"PATH"})

    def test_windows_tool_path_is_preserved(self):
        env = {"PATH": "trusted-windows-path"}
        with mock.patch.object(snapshot.os, "name", "nt"):
            snapshot.system({"env": env})
        self.assertEqual(env["PATH"], "trusted-windows-path")


class Home(unittest.TestCase):
    def test_snapshot_data_and_windows_homes_are_owned_and_ignore_ambient_state(self):
        ambient = {"HOME": "/host/home", "USERPROFILE": "/host/profile", "PLUMB_HOME": "/host/plumb", "APPDATA": "/host/apps", "LOCALAPPDATA": "/host/local", "PATH": "/trusted/bin"}
        with mock.patch.dict(os.environ, ambient, clear=True), tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            release = snapshot.environment(home, {"rust.version": "1.96.1"})
            self.assertEqual(release["HOME"], temporary)
            self.assertEqual(release["USERPROFILE"], temporary)
            self.assertEqual(release["PLUMB_HOME"], str(home / ".plumb"))
            self.assertNotIn("APPDATA", release)
            self.assertNotIn("LOCALAPPDATA", release)
            self.assertEqual(release["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(release["RUSTUP_TOOLCHAIN"], "1.96.1")
            self.assertNotIn("USERPROFILE", evidence.clean(os.environ, home))
            self.assertNotIn("PLUMB_HOME", evidence.clean(os.environ, home))


class Tools(unittest.TestCase):
    def test_refusal_names_only_tool_and_status_without_child_output(self):
        done = mock.Mock(returncode=2, stdout="PRIVATE_CHILD_OUTPUT")
        with mock.patch.object(evidence.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(Refusal, "static tool plumb refused with exit status 2") as caught:
                evidence.inspect(["/trusted/plumb", "guard", "PRIVATE_ARGUMENT"], "/source", {})
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_output_budget_is_distinct_from_tool_refusal(self):
        done = mock.Mock(returncode=0, stdout="x" * (evidence.LIMIT + 1))
        with mock.patch.object(evidence.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(Refusal, "exceeded its output budget"):
                evidence.inspect(["/trusted/plumb"], "/source", {})
