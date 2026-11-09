import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.package.source import original
from lib.process.promotion import preflight
from lib.refusal import Refusal
from lib.store import plan
from tests.confirmation.semantics.test_records import AUTHORITY, NATIVE, Store
from tests.promotion.preparation.test_capture import SOURCE, world


class OriginalSource(unittest.TestCase):
    def setUp(self):
        self.world = world()
        self.document = {"schema": plan.SCHEMA, "context": {**SOURCE, "run": "7", "attempt": "1", "wharf": "e" * 40, "snapshot": self.world}}
        self.distribution = {**SOURCE, "plan": plan.reference(self.document), "resolution": {"fixture": plan.combination(self.world)}}
        self.reference = {"url": "https://releases.plumb.perish.uk/distribution.json", "sha256": "a" * 64, "size": 10}
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "original", "recovery": []}
        self.payload = {"schema": "wharf.release.source/v1", "fixture": "complete verified tracked bytes"}
        self.store = Store()

    def held(self, directory):
        return {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "observer": "{}", "source": str(Path(directory) / "original")}

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (original.capture, "active", {"return_value": NATIVE}),
            (original.preflight, "event", {"side_effect": lambda: {"semantics": canonical.encode(self.request).decode()}}),
            (original.capture, "published", {"side_effect": lambda held: (copy.deepcopy(self.distribution), self.reference)}),
            (original.capture, "original", {"side_effect": lambda reference: copy.deepcopy(self.document)}),
            (original.capture, "original_native", {}),
            (original.capture, "inspect", {"return_value": (AUTHORITY, {"authentication": False, "publication": False})}),
            (original.reconstruct, "recover", {"side_effect": lambda *args: copy.deepcopy(self.payload)}),
            (original.reader, "identity", {"return_value": AUTHORITY}),
            (original.r2, "writer", {"return_value": self.store}),
            (original.release, "fetch", {"side_effect": self.store.public}),
            (original.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def test_preparation_and_confirmation_have_no_writer_or_later_workload_access(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            with mock.patch.object(original.r2, "writer", side_effect=AssertionError("preparation has no release writer")):
                original.prepare(held)
            with mock.patch.object(original.capture, "original", side_effect=AssertionError("no workload credential after prepare")):
                intent = original.produce(held)
                with mock.patch.object(original.r2, "writer", side_effect=AssertionError("confirmation has no writer")):
                    confirmed = original.confirm(held | {"digest": intent["reference"]["sha256"]})
            self.assertFalse(confirmed["publication"])
            self.assertFalse(confirmed["authentication"])
            self.assertEqual(len(self.store.values), 3)

    def test_changed_saved_plan_cannot_relabel_original_world(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            original.prepare(held)
            path = Path(held["source"]) / "plan.json"
            document = json.loads(path.read_bytes())
            document["context"]["snapshot"]["head"] = "c" * 40
            path.write_bytes(canonical.encode(document))
            with self.assertRaisesRegex(Refusal, "exact public reference"):
                original.produce(held)
        self.assertFalse(self.store.values)

    def test_current_publication_or_formal_reader_movement_refuses_before_write(self):
        for changed in ("publication", "reader"):
            self.setUp()
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory, self.patches():
                held = self.held(directory)
                original.prepare(held)
                if changed == "publication":
                    self.distribution["plan"]["sha256"] = "f" * 64
                else:
                    self.payload["moved"] = True
                with self.assertRaises(Refusal):
                    original.produce(held)
                self.assertFalse(self.store.values)

    def test_unknown_write_keeps_intent_and_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            original.prepare(held)
            with mock.patch.object(self.store, "create", side_effect=OSError("unknown acknowledgement")) as create, self.assertRaises(OSError):
                original.produce(held)
            create.assert_called_once()
            self.assertTrue((Path(held["source"]) / "intent.json").is_file())
            with self.assertRaisesRegex(Refusal, "receipt set"):
                original.produce(held)

    def test_confirmation_refuses_public_source_changed_after_retention(self):
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            original.prepare(held)
            intent = original.produce(held)
            ref = intent["record"]["material"]
            self.store.values["v1/semantics/objects/" + ref["sha256"] + ".json"] = b"{}"
            with self.assertRaisesRegex(Refusal, "public bytes differ"):
                original.confirm(held | {"digest": intent["reference"]["sha256"]})

    def test_original_request_is_stable_strict_and_excludes_publication_modes(self):
        raw = canonical.encode(self.request).decode()
        self.assertEqual(preflight.semantics(raw), self.request)
        selected = {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "semantics": raw}
        for changed in ({"marker": "v1.2.3-rc.1"}, {"prepare": True}, {"candidate": True}, {"proof": True}, {"run": "7"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                preflight.selection(selected | changed)
