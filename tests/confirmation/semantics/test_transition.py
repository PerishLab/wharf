import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.confirmation import semantics
from lib.refusal import Refusal
from tests.confirmation.semantics.test_records import AUTHORITY, NATIVE, Store
from tests.promotion.preparation.test_capture import SOURCE, world


class Transition(unittest.TestCase):
    def setUp(self):
        self.world = world()
        self.worldref = semantics.object_reference("plumb", canonical.encode(self.world))
        self.prior = {"url": "https://releases.plumb.perish.uk/v1/semantics/objects/" + "a" * 64 + ".json", "sha256": "a" * 64, "size": 12}
        intent = {"product": "plumb", "marker": SOURCE["marker"], "root": self.prior, "previous": self.prior, "anchor": self.prior, "request": {}, "scenes": {}, "world": self.worldref, "review": self.prior, "direct": None}
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "transition", "mode": "historical", "record": {"schema": "wharf.release.semantics.transition/v1", "intent": intent, "history": self.prior, "source": None}}
        self.selection = {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "semantics": canonical.encode(self.request).decode()}
        self.distribution = {**SOURCE, "plan": {"fixture": "original-plan"}}
        self.original = {"url": "https://releases.plumb.perish.uk/distribution.json", "sha256": "b" * 64, "size": 12}
        self.store = Store()

    def held(self, directory):
        return {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "observer": "{}", "source": str(Path(directory) / "transition")}

    def protocol(self, arguments, budget=180):
        self.assertEqual(arguments[4:], ["--purpose", "historical" if self.request["mode"] == "historical" else "transition", "--product", "plumb"])
        raw = Path(arguments[3]).read_bytes()
        record = json.loads(raw)
        self.assertEqual(record["intent"]["native"], NATIVE)
        report = {"schema": "plumb.release.transition.materials/v1", "freshness": self.request["mode"], "authentication": False, "publication": False, "previous": {"reference": record["intent"]["previous"], "authentication": True, "publication": False}, "derived": {"reference": semantics.object_reference("plumb", raw), "authentication": False, "publication": False}}
        return 0, {"state": "observed", "facts": report, "publication": False}

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (semantics.capture, "active", {"return_value": NATIVE}),
            (semantics.preflight, "event", {"return_value": self.selection}),
            (semantics.capture, "published", {"return_value": (self.distribution, self.original)}),
            (semantics.capture, "original", {"return_value": {}}),
            (semantics.capture, "snapshot", {"return_value": (self.world, canonical.encode(self.world))}),
            (semantics.reader, "identity", {"return_value": AUTHORITY}),
            (semantics.reader, "capture", {"side_effect": self.protocol}),
            (semantics.r2, "writer", {"return_value": self.store}),
            (semantics.release, "fetch", {"side_effect": self.store.public}),
            (semantics.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def test_exact_issuance_and_read_only_confirmation(self):
        for mode in ("historical", "current"):
            with self.subTest(mode=mode):
                self.setUp()
                self.request["mode"] = mode
                self.selection["semantics"] = canonical.encode(self.request).decode()
                with tempfile.TemporaryDirectory() as directory, self.patches():
                    held = self.held(directory)
                    pending = semantics.transition(held)
                    self.assertFalse(pending["authentication"])
                    self.assertFalse(pending["publication"])
                    self.assertEqual(pending["native"], NATIVE)
                    with mock.patch.object(semantics.r2, "writer", side_effect=AssertionError("confirmation has no writer")), mock.patch.object(semantics.capture, "original", side_effect=AssertionError("confirmation has no workload reader")):
                        confirmed = semantics.confirm(held | {"digest": pending["reference"]["sha256"]})
                    self.assertFalse(confirmed["authentication"])
                    self.assertEqual(len(self.store.values), 2)
                    self.assertTrue((Path(held["source"]) / "confirmation.json").is_file())

    def test_original_world_mismatch_refuses_before_store(self):
        self.request["record"]["intent"]["world"] = self.prior
        self.selection["semantics"] = canonical.encode(self.request).decode()
        with tempfile.TemporaryDirectory() as directory, self.patches(), self.assertRaisesRegex(Refusal, "original captured publication"):
            semantics.transition(self.held(directory))
        self.assertFalse(self.store.values)

    def test_failed_materials_leave_only_unissued_world_and_intent(self):
        with tempfile.TemporaryDirectory() as directory, self.patches(), mock.patch.object(semantics.reader, "capture", return_value=(3, {"state": "unknown", "publication": False, "reason": "original family unavailable"})), self.assertRaisesRegex(Refusal, "original family unavailable"):
            held = self.held(directory)
            try:
                semantics.transition(held)
            finally:
                self.assertTrue((Path(held["source"]) / "intent.json").is_file())
                self.assertFalse((Path(held["source"]) / "reference.json").exists())
        self.assertEqual(len(self.store.values), 1)

    def test_excess_authority_or_wrong_freshness_never_issues(self):
        for field, value in (("authentication", True), ("publication", True), ("freshness", "current"), ("derived", {"authentication": True}), ("previous", None)):
            self.setUp()
            original = self.protocol
            def changed(arguments, budget=180):
                status, report = original(arguments)
                report["facts"][field] = value
                return status, report
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, self.patches(), mock.patch.object(semantics.reader, "capture", side_effect=changed), self.assertRaises(Refusal):
                semantics.transition(self.held(directory))
            self.assertEqual(len(self.store.values), 1)

    def test_confirmation_refuses_changed_input_or_materials(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            pending = semantics.transition(held)
            path = Path(held["source"]) / "materials.json"
            report = json.loads(path.read_bytes())
            report["freshness"] = "current"
            path.write_bytes(canonical.encode(report))
            with self.assertRaisesRegex(Refusal, "facts moved"):
                semantics.confirm(held | {"digest": pending["reference"]["sha256"]})

    def test_unknown_record_write_retains_intent_without_confirmation(self):
        create = self.store.create
        def uncertain(key, raw, headers=None):
            create(key, raw, headers)
            if json.loads(raw).get("schema") == "wharf.release.semantics.transition/v1":
                raise OSError("unknown record acknowledgement")
        with tempfile.TemporaryDirectory() as directory, self.patches(), mock.patch.object(self.store, "create", side_effect=uncertain), self.assertRaises(OSError):
            held = self.held(directory)
            try:
                semantics.transition(held)
            finally:
                self.assertTrue((Path(held["source"]) / "intent.json").is_file())
                self.assertFalse((Path(held["source"]) / "confirmation.json").exists())
        self.assertEqual(len(self.store.values), 2)

    def test_reader_budget_is_bounded_before_process_creation(self):
        for budget in (0, -1, 901, True, None):
            with self.subTest(budget=budget), mock.patch.object(semantics.reader.subprocess, "Popen") as spawn, self.assertRaises(Refusal):
                try:
                    semantics.reader.capture(["release", "inspect"], budget)
                finally:
                    spawn.assert_not_called()

    def test_confirmation_validates_regular_receipts_before_reading_them(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            pending = semantics.transition(held)
            path = Path(held["source"]) / "world.json"
            path.unlink()
            path.symlink_to(Path(directory) / "absent")
            with self.assertRaisesRegex(Refusal, "receipt set differs"):
                semantics.confirm(held | {"digest": pending["reference"]["sha256"]})
