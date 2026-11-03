import json
import unittest
from types import SimpleNamespace
from unittest import mock

from lib.identity.guard import snapshot
from lib.refusal import Refusal
from tests.lib.cargo import test_replay as fixtures


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
