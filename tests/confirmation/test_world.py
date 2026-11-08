import copy
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.confirmation import world
from lib.refusal import Refusal
from tests.promotion.preparation.test_capture import SOURCE, world as original

NATIVE = {"run": 9, "attempt": 1, "commit": "e" * 40, "job": 19}


class World(unittest.TestCase):
    def test_product_source_must_be_held_by_its_protected_native_main(self):
        prefix = "repos/" + SOURCE["repository"]
        commit = SOURCE["commit"]
        head = "c" * 40
        observations = {
            prefix: {"full_name": SOURCE["repository"], "visibility": "public", "archived": False, "disabled": False, "owner": {"id": 330236468, "node_id": "O_kgDOE68CNA"}, "default_branch": "main"},
            prefix + "/branches/main": {"name": "main", "protected": True, "commit": {"sha": head}},
            prefix + f"/compare/{commit}...{head}": {"base_commit": {"sha": commit}, "merge_base_commit": {"sha": commit}, "status": "ahead"},
            prefix + "/git/commits/" + commit: {"sha": commit, "tree": {"sha": SOURCE["tree"]}},
        }
        with mock.patch.object(world.subject, "api", side_effect=observations.__getitem__):
            self.assertEqual(world.identity(SOURCE, {"commit": commit}), SOURCE)
        for path, field, value in [(prefix, "owner", {"id": 1}), (prefix + "/branches/main", "protected", False), (prefix + f"/compare/{commit}...{head}", "merge_base_commit", {"sha": "0" * 40}), (prefix + "/git/commits/" + commit, "sha", "0" * 40)]:
            changed = copy.deepcopy(observations)
            changed[path][field] = value
            with self.subTest(field=field), mock.patch.object(world.subject, "api", side_effect=changed.__getitem__), self.assertRaises(Refusal):
                world.identity(SOURCE, {"commit": commit})

    def fixture(self):
        self.world = original()
        self.authority = self.world["controls"]["authority"]
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "world", "commit": SOURCE["commit"], "scenes": {"fixture": "selected-current-Scene"}}
        self.collection = {"schema": "plumb.release.collection/v1", "state": "observed", "snapshot": {"product": "plumb", "source": {name: SOURCE[name] for name in ("repository", "commit", "tree")}}, "adoption": False, "publication": False}
        self.payload = {"schema": "wharf.release.source/v1", "source": SOURCE, "head": self.world["head"], "tree": self.world["tree"], "files": 1, "payload": {"fixture": "separately exercised real Git acquisition"}}
        self.unchanged = mock.Mock()
        self.prepared = mock.Mock(side_effect=self.context)

    @contextmanager
    def context(self, request):
        self.assertIsNone(request.expected)
        self.assertIsNone(request.recall)
        self.assertIsNone(request.pull)
        self.assertEqual(request.identity, SOURCE)
        yield Path("/fixture/effective-source"), self.world, self.unchanged

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (world.capture, "active", {"return_value": NATIVE}),
            (world.preflight, "event", {"return_value": {"semantics": canonical.encode(self.request).decode()}}),
            (world, "identity", {"return_value": SOURCE}),
            (world.reader, "identity", {"return_value": self.authority}),
            (world.semantics, "collected", {"return_value": self.collection}),
            (world.toolchain, "versions", {"return_value": self.world["domain"]}),
            (world.snapshot, "prepared", {"side_effect": self.prepared}),
            (world.materials, "capture", {"return_value": self.payload}),
            (world.capture, "inspect", {"side_effect": lambda data, raw: (self.authority, {"state": "observed", "facts": {"data": data, "authentication": False, "publication": False}, "publication": False})}),
            (world.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def held(self, directory):
        return {"observer": {}, "repository": SOURCE["repository"], "marker": SOURCE["marker"], "source": str(Path(directory) / "product"), "artifact": str(Path(directory) / "artifact")}

    def test_actual_carrier_and_confirmation_bind_both_digests_without_publication(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selected = world.source(held)
            self.assertEqual(selected["native"], NATIVE)
            result = world.produce(held)
            self.assertFalse(any(result[name] for name in ("adoption", "qualification", "activation", "publication")))
            self.prepared.assert_called_once()
            self.unchanged.assert_called_once()
            expected = {"world": result["world"]["sha256"], "source": result["material"]["sha256"]}
            world.parameters.answer.assert_called_with({"digest": expected["world"], "payload": expected["source"]})
            confirmed = world.confirm(held | {"observation": canonical.encode(expected).decode()})
            self.assertFalse(confirmed["publication"])
            self.assertEqual(confirmed["material"], result["material"])
            self.assertEqual(len(list(Path(held["artifact"]).iterdir())), 7)

    def test_failed_actual_guard_retains_selection_without_world_receipt(self):
        self.fixture()
        self.prepared.side_effect = Refusal("actual Guard did not pass")
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            world.source(held)
            with self.assertRaisesRegex(Refusal, "Guard did not pass"):
                world.produce(held)
            self.assertEqual({path.name for path in Path(held["artifact"]).iterdir()}, {"selection.json", "collection.json"})

    def test_unknown_or_moved_control_never_creates_a_preparation(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            world.source(held)
            with mock.patch.object(world.reader, "identity", return_value={}), self.assertRaises(Refusal):
                world.produce(held)
            self.prepared.assert_not_called()

    def test_tampered_source_or_invented_confirmation_digest_refuse(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            world.source(held)
            result = world.produce(held)
            expected = {"world": result["world"]["sha256"], "source": result["material"]["sha256"]}
            with self.assertRaises(Refusal):
                world.confirm(held | {"observation": canonical.encode(expected | {"source": "0" * 64}).decode()})
            (Path(held["artifact"]) / "source.json").write_bytes(b"foreign")
            with self.assertRaises(Refusal):
                world.confirm(held | {"observation": canonical.encode(expected).decode()})

    def test_world_from_other_source_or_control_cannot_use_preparation_role(self):
        for field in ("source", "controls"):
            self.fixture()
            self.world[field] = copy.deepcopy(self.world[field])
            if field == "source":
                self.world[field]["commit"] = "0" * 40
            else:
                self.world[field]["authority"] = {}
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, self.patches():
                held = self.held(directory)
                world.source(held)
                with self.assertRaises(Refusal):
                    world.produce(held)
