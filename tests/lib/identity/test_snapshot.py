import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lib.identity.guard import execution, snapshot
from lib.process import git
from lib.refusal import Refusal
from tests.lib.media.repository import Repository


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
