import json
import unittest
import unittest.mock
from types import SimpleNamespace

from lib.content import canonical
from lib.identity.guard import replay
from lib.store import plan
from lib.process import git
from lib.refusal import Refusal
from tests.lib.cargo.test_basis import Repository

OWN = """
[[package]]
name = "demo"
version = "0.9.0"
source = "sparse+https://cargo.perish.uk/"
checksum = "eeee"
"""


def index(**versions):
    def reader(url):
        name = url.rsplit("/", 1)[1]
        return "\n".join(json.dumps({"name": name, "vers": version, "yanked": False}) for version in versions.get(name, []))
    return reader


class Replay(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.repository.edit("Cargo.lock", 'dependencies = ["demo"]', 'dependencies = ["demo 0.0.0", "demo 0.9.0"]')
        self.repository.write("Cargo.lock", (self.repository.root / "Cargo.lock").read_text() + OWN)
        self.repository.commit()
        self.root = self.repository.root
        self.expected = {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}]}

    def held(self, expected):
        request = SimpleNamespace(expected=expected, identity={"marker": "v1.0.0"})
        return {"request": request, "root": self.root, "tools": {"cargo": {"path": "cargo"}, "git": {"path": "git"}}}

    def test_own_library_published_by_this_marker_still_replays(self):
        replay.current(self.root, self.expected, "1.0.0", index(demo=["0.9.0", "1.0.0-rc.1", "1.0.0"]))

    def test_foreign_latest_movement_refuses(self):
        expected = {"packages": self.expected["packages"] + [{"ecosystem": "cargo", "name": "plumb", "version": "0.1.0"}]}
        with self.assertRaisesRegex(Refusal, "plumb moved to 0.2.0; prepare a fresh plan"):
            replay.current(self.root, expected, "1.0.0", index(demo=["1.0.0"], plumb=["0.1.0", "0.2.0"]))

    def test_own_library_moving_elsewhere_refuses(self):
        with self.assertRaisesRegex(Refusal, "demo moved to 1.1.0"):
            replay.current(self.root, self.expected, "1.0.0", index(demo=["0.9.0", "1.1.0"]))

    def test_replay_keeps_the_planned_lock_and_tree(self):
        calls = []
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        execute = lambda held, argv: calls.append(argv)
        run = lambda held, *args: git(self.root, *args).strip()
        with unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0", "1.0.0"])):
            result = replay.resolved(self.held(self.expected), execute, run)
        self.assertEqual(calls, [])
        self.assertEqual(result, {"context": "lift", "tree": tree, "packages": self.expected["packages"]})

    def test_replay_moves_a_package_the_plan_resolved_elsewhere(self):
        expected = {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.1"}]}
        self.assertEqual(replay.moves(self.root, expected), [("sparse+https://cargo.perish.uk/#demo@0.9.0", "0.9.1")])

    def test_applies_only_to_planned_cargo_snapshots(self):
        self.assertTrue(replay.applies(self.held(self.expected)))
        self.assertFalse(replay.applies(self.held(None)))
        self.assertFalse(replay.applies(self.held({"packages": [{"ecosystem": "npm", "name": "x", "version": "1"}]})))


class Reserved(unittest.TestCase):
    def setUp(self):
        from tests.lib.store.memory import Memory
        self.bucket = Memory()
        self.context = {"repository": "PerishLab/demo", "marker": "v1.0.0", "run": "9", "attempt": "1"}
        self.combination = {"schema": "wharf.release.snapshot/v1", "source": {"repository": "PerishLab/demo", "marker": "v1.0.0", "commit": "a" * 40, "tree": "b" * 40}, "head": "c" * 40, "tree": "d" * 40, "packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}], "controls": {}, "domain": {}}

    def test_a_marker_that_began_publication_replans_its_reserved_combination(self):
        self.bucket.create(plan.publication(self.context), canonical.encode(self.combination))
        plan.remember(self.bucket, dict(self.context, snapshot=dict(self.combination, guard={"digest": "e"})))
        recalled = {}
        self.assertEqual(plan.reserved(self.bucket, self.context, recalled), dict(self.combination, guard={"digest": "e"}))
        self.assertEqual(recalled["run"], "9")

    def test_a_fresh_marker_resolves_latest(self):
        self.assertIsNone(plan.reserved(self.bucket, self.context, {}))


class Mixed(unittest.TestCase):
    def setUp(self):
        fixture = Replay()
        fixture.setUp()
        self.root = fixture.root
        self.expected = fixture.expected
        fixture.repository.write("plumb.toml", '[release.npm]\npackages = ["@perishlab/owned"]\n')
        fixture.repository.commit()
        self.expected["packages"].append({"ecosystem": "npm", "name": "@perishlab/demo", "version": "0.9.0"})
        self.expected["tree"] = git(self.root, "rev-parse", "HEAD^{tree}").strip()
        self.calls = []

    def held(self, expected):
        held = Replay.held(self, expected)
        held["tools"]["pnpm"] = {"path": "/prepared/pnpm"}
        held["env"] = {"USERPROFILE": str(self.root.parent)}
        return held

    def execute(self, held, argv):
        self.calls.append((held["root"], argv))
        return json.dumps("0.9.0")

    def resolve(self):
        run = lambda held, *args: git(self.root, *args).strip()
        with unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0"])):
            return replay.resolved(self.held(self.expected), self.execute, run)

    def test_mixed_replay_uses_prepared_metadata_reader_outside_source(self):
        self.assertTrue(replay.applies(self.held(self.expected)))
        self.assertEqual(self.resolve()["tree"], self.expected["tree"])
        self.assertEqual(len(self.calls), 1)
        cwd, argv = self.calls[0]
        self.assertEqual(cwd, self.root.parent)
        self.assertEqual(argv[0], "/prepared/pnpm")
        self.assertIn("--config.ignore-pnpmfile=true", argv)
        self.assertIn("--config.ignore-scripts=true", argv)
        self.assertEqual(argv[-4:], ["view", "@perishlab/demo@latest", "version", "--json"])

    def test_mixed_replay_refuses_a_different_planned_tree(self):
        self.expected["tree"] = "0" * 40
        with self.assertRaisesRegex(Refusal, "exact planned npm"):
            self.resolve()

    def test_mixed_replay_refuses_foreign_latest_drift(self):
        self.expected["packages"][-1]["version"] = "0.8.0"
        with self.assertRaisesRegex(Refusal, "moved; prepare a fresh plan"):
            self.resolve()

    def test_mixed_replay_refuses_unknown_registry_before_execution(self):
        self.expected["packages"][-1]["name"] = "@foreign/demo"
        with self.assertRaisesRegex(Refusal, "unsupported npm registry"):
            self.resolve()
        self.assertEqual(self.calls, [])

    def test_mixed_replay_refuses_nonstable_metadata(self):
        run = lambda held, *args: git(self.root, *args).strip()
        for result in ("null", '"1.0.0-rc.1"', '["0.9.0"]'):
            with self.subTest(result=result), unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0"])):
                with self.assertRaisesRegex(Refusal, "current stable npm metadata"):
                    replay.resolved(self.held(self.expected), lambda held, argv: result, run)
