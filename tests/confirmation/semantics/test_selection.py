import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.promotion import selection
from lib.refusal import Conflict, Refusal
from tests.confirmation.semantics.test_records import AUTHORITY, NATIVE, Store
from tests.promotion.preparation.test_capture import SOURCE


class HeadStore(Store):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.unknown = None

    def create(self, key, raw, headers=None):
        self.calls.append(("create", key))
        super().create(key, raw, headers)
        if self.unknown == key:
            raise OSError("unknown acknowledgement")

    def swap(self, key, raw, etag):
        self.calls.append(("swap", key))
        if self.snapshot(key)[1] != etag:
            raise Conflict("predecessor changed")
        self.values[key] = raw
        if self.unknown == key:
            raise OSError("unknown acknowledgement")

    def public(self, url):
        return self.get(url.split(".perish.uk/", 1)[1])


class Selection(unittest.TestCase):
    def setUp(self):
        self.reference = {"url": "https://releases.plumb.perish.uk/v1/semantics/objects/" + "b" * 64 + ".json", "sha256": "b" * 64, "size": 100}
        self.record = {"schema": "wharf.release.semantics.family.selection/v1", "product": "plumb", "marker": SOURCE["marker"], "root": self.reference, "family": self.reference, "history": self.reference, "previous": None, "sequence": 1}
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "selection", "record": self.record}
        self.store = HeadStore()
        self.first = None
        self.reads = 0

    def held(self, directory):
        return {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "observer": "{}", "source": str(Path(directory) / "selection")}

    def protocol(self, arguments):
        self.reads += 1
        self.assertEqual(arguments[4:], ["--purpose", "nomination", "--product", "plumb"])
        record = json.loads(Path(arguments[3]).read_bytes())
        self.assertEqual(record["native"], NATIVE)
        report = {"schema": "plumb.release.selection.materials/v1", "selection": record, "family": {"authentication": True, "publication": False, "reference": record["family"], "root": record["root"], "stable": {"world": {"source": SOURCE}}}, "history": {"authentication": True, "publication": False, "reference": record["history"]}, "predecessors": [], "inception": self.first, "authentication": False, "publication": False}
        return 0, {"state": "observed", "facts": report, "publication": False}

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (selection.capture, "active", {"return_value": NATIVE}),
            (selection.preflight, "event", {"side_effect": lambda: {"semantics": canonical.encode(self.request).decode()}}),
            (selection.reader, "identity", {"return_value": AUTHORITY}),
            (selection.reader, "select", {"side_effect": self.protocol}),
            (selection.r2, "writer", {"return_value": self.store}),
            (selection.release, "fetch", {"side_effect": self.store.public}),
            (selection.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def publish(self, held):
        with mock.patch.object(selection.r2, "writer", side_effect=AssertionError("preparation has no store writer")):
            prepared = selection.prepare(held)
        self.assertFalse(prepared["authentication"])
        return selection.produce(held)

    def test_first_selection_and_read_only_confirmation(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            intent = self.publish(held)
            self.assertFalse(intent["authentication"])
            self.assertFalse(intent["publication"])
            self.assertEqual(self.store.calls[-2:], [("create", selection.FIRST), ("swap", selection.HEAD)])
            self.first = json.loads(self.store.values[selection.FIRST])
            with mock.patch.object(selection.r2, "writer", side_effect=AssertionError("confirmation has no writer")):
                confirmed = selection.confirm(held | {"digest": intent["reference"]["sha256"]})
            self.assertFalse(confirmed["authentication"])
            self.assertEqual(self.reads, 3)

    def test_successor_preserves_inception_and_updates_exact_predecessor(self):
        self.first = {"schema": selection.SCHEMA, "reference": self.reference}
        before = canonical.encode(self.first)
        self.store.values = {selection.FIRST: before, selection.HEAD: before}
        self.record.update(previous=self.reference, sequence=2)
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            intent = self.publish(held)
            self.assertEqual(self.store.values[selection.FIRST], before)
            self.assertEqual(self.store.calls[-1], ("swap", selection.HEAD))
            self.assertNotIn(("create", selection.FIRST), self.store.calls)
            with mock.patch.object(selection.r2, "writer", side_effect=AssertionError("confirmation has no writer")):
                selection.confirm(held | {"digest": intent["reference"]["sha256"]})

    def test_lost_head_never_reinitializes_existing_inception(self):
        self.store.values[selection.FIRST] = canonical.encode({"schema": selection.SCHEMA, "reference": self.reference})
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            with self.assertRaisesRegex(Refusal, "already initialized"):
                selection.produce(held)
        self.assertEqual(self.store.calls, [])

    def test_material_unknown_stops_before_store(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            with mock.patch.object(selection.reader, "select", return_value=(3, {"state": "unknown", "publication": False, "reason": "history unavailable"})), self.assertRaisesRegex(Refusal, "history unavailable"):
                selection.produce(held)
        self.assertEqual(self.store.calls, [])

    def test_changed_materials_stop_before_store(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            status, changed = self.protocol(["release", "inspect", "--input", str(Path(held["source"]) / "record.json"), "--purpose", "nomination", "--product", "plumb"])
            changed["facts"]["predecessors"] = [{"changed": True}]
            with mock.patch.object(selection.reader, "select", return_value=(status, changed)), self.assertRaisesRegex(Refusal, "moved since preparation"):
                selection.produce(held)
        self.assertEqual(self.store.calls, [])

    def test_unknown_writes_are_not_replayed_and_keep_intent(self):
        for key in (selection.FIRST, selection.HEAD):
            with self.subTest(key=key):
                self.setUp()
                self.store.unknown = key
                with tempfile.TemporaryDirectory() as directory, self.patches():
                    held = self.held(directory)
                    selection.prepare(held)
                    with self.assertRaisesRegex(OSError, "unknown acknowledgement"):
                        selection.produce(held)
                    self.assertTrue((Path(held["source"]) / "intent.json").is_file())
                    self.assertEqual(sum(name == key for _, name in self.store.calls), 1)
                    before = list(self.store.calls)
                    with self.assertRaisesRegex(Refusal, "receipt set"):
                        selection.produce(held)
                    self.assertEqual(self.store.calls, before)

    def test_concurrent_head_movement_refuses_cas_without_rollback(self):
        self.first = {"schema": selection.SCHEMA, "reference": self.reference}
        self.store.values = {selection.FIRST: canonical.encode(self.first), selection.HEAD: canonical.encode(self.first)}
        self.record.update(previous=self.reference, sequence=2)
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            with mock.patch.object(self.store, "swap", side_effect=Conflict("concurrent head moved")) as swap, self.assertRaisesRegex(Conflict, "concurrent head moved"):
                selection.produce(held)
            swap.assert_called_once()
            self.assertEqual(self.store.values[selection.FIRST], canonical.encode(self.first))
            self.assertTrue((Path(held["source"]) / "intent.json").is_file())
            self.assertFalse((Path(held["source"]) / "confirmation.json").exists())

    def test_wrong_predecessor_stops_before_store(self):
        self.record.update(previous=self.reference, sequence=2)
        self.first = {"schema": selection.SCHEMA, "reference": self.reference}
        self.store.values[selection.FIRST] = canonical.encode(self.first)
        self.store.values[selection.HEAD] = canonical.encode(self.first | {"reference": self.reference | {"sha256": "c" * 64}})
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            with self.assertRaisesRegex(Refusal, "exact predecessor"):
                selection.produce(held)
        self.assertEqual(self.store.calls, [])

    def test_native_request_moves_after_preparation(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            selection.prepare(held)
            self.record["root"] = self.reference | {"sha256": "c" * 64}
            with self.assertRaisesRegex(Refusal, "differs from actual request"):
                selection.produce(held)
        self.assertEqual(self.store.calls, [])

    def test_confirmation_refuses_moved_public_head(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            intent = self.publish(held)
            self.first = json.loads(self.store.values[selection.FIRST])
            self.store.values[selection.HEAD] = b"{}"
            with self.assertRaisesRegex(Refusal, "public selection"):
                selection.confirm(held | {"digest": intent["reference"]["sha256"]})
            self.assertFalse((Path(held["source"]) / "confirmation.json").exists())

    def test_caller_native_or_unknown_field_refuses(self):
        for name in ("native", "authentication", "foreign"):
            request = copy.deepcopy(self.request)
            request["record"][name] = NATIVE
            with self.assertRaises(Refusal):
                selection.preflight.semantics(canonical.encode(request).decode())
