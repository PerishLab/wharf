import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind.spec import entry
from lib.process.confirmation import semantics
from lib.refusal import Conflict, Refusal
from tests.confirmation.fixtures import Ledger
from tests.promotion.preparation.test_capture import SOURCE, world

NATIVE = {"run": 9, "attempt": 1, "commit": "e" * 40, "job": 19}
AUTHORITY = {"producer": "v0.140.0@" + "d" * 40, "depot": "d" * 64}
LOCATION = "https://github.com/PerishLab/seatrial/issues/75#issuecomment-7"


class Store:
    def __init__(self):
        self.values = {}

    def create(self, key, raw, headers=None):
        if key in self.values:
            raise Conflict("exists")
        self.values[key] = raw

    def get(self, key):
        return self.values[key]

    def snapshot(self, key):
        return self.values.get(key), "etag" if key in self.values else None

    def public(self, url):
        return self.get("v1/semantics/objects/" + url.rsplit("/", 1)[1])


class Semantics(unittest.TestCase):
    def fixture(self):
        self.world = world()
        self.scenes = {"fixture": "selected-current-scene"}
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "adoption", "scenes": self.scenes, "review": LOCATION}
        self.selection = {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "semantics": canonical.encode(self.request).decode()}
        self.snapshot = {"product": "plumb", "source": {name: SOURCE[name] for name in ("repository", "commit", "tree")}, "materials": [], "coverage": []}
        self.distribution = {"fixture": "exact-complete-original-distribution"}
        self.original = {"url": "https://releases.plumb.perish.uk/distribution.json", "sha256": "a" * 64, "size": 42}
        self.review = {"reference": {"url": LOCATION, "sha256": "b" * 64, "size": 123}, "judgment": {"product": "plumb", "marker": SOURCE["marker"], "world": entry(canonical.encode(self.world))["sha256"]}}
        self.store = Store()

    def protocol(self, arguments):
        if arguments[:2] == ["release", "collect"]:
            return 0, {"schema": "plumb.release.collection/v1", "state": "observed", "adoption": False, "publication": False, "snapshot": self.snapshot, "observation": {"source": self.snapshot["source"], "scenes": {"pointer": self.scenes}}}
        if arguments[:2] == ["release", "review"]:
            return 0, {"state": "observed", "publication": False, "facts": self.review}
        self.assertEqual(arguments[4:], ["--purpose", "materials", "--product", "plumb"])
        record = json.loads(Path(arguments[3]).read_bytes())
        report = {"schema": "plumb.release.materials/v1", "authentication": False, "publication": False,
                  "data": {"world": self.world, "collection": {"snapshot": record["release"]["snapshot"]}, "review": {"reference": record["review"]}, "published": {"distribution": self.distribution}}}
        return 0, {"state": "observed", "publication": False, "facts": report}

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (semantics.capture, "active", {"return_value": NATIVE}),
            (semantics.preflight, "event", {"return_value": self.selection}),
            (semantics.capture, "published", {"return_value": (self.distribution, self.original)}),
            (semantics.capture, "original", {"return_value": {}}),
            (semantics.capture, "snapshot", {"return_value": (self.world, canonical.encode(self.world))}),
            (semantics.capture, "inspect", {"return_value": (AUTHORITY, {"fixture": "world-data"})}),
            (semantics.reader, "identity", {"return_value": AUTHORITY}),
            (semantics.reader, "capture", {"side_effect": self.protocol}),
            (semantics.r2, "writer", {"return_value": self.store}),
            (semantics.release, "fetch", {"side_effect": self.store.public}),
            (semantics.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def held(self, directory):
        return {"observer": {}, "repository": SOURCE["repository"], "marker": SOURCE["marker"], "source": str(Path(directory) / "adoption")}

    def test_issue_and_exact_confirmation_share_materials_and_keep_publication_false(self):
        self.fixture()
        self.distribution["plan"] = {"fixture": "original"}
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            result = semantics.issue(held)
            self.assertFalse(result["authentication"] or result["publication"])
            record = json.loads((Path(held["source"]) / "record.json").read_bytes())
            self.assertEqual(record["native"], NATIVE)
            self.assertEqual(record["release"]["snapshot"], self.snapshot)
            self.assertEqual(len(self.store.values), 2)
            semantics.parameters.answer.assert_called_once_with({"digest": result["reference"]["sha256"]})
            confirmed = semantics.confirm(held | {"digest": result["reference"]["sha256"]})
            self.assertEqual(confirmed["reference"], result["reference"])
            self.assertFalse(confirmed["publication"])
            semantics.r2.writer.assert_called_once_with("perish-plumb-releases", "RELEASES")

    def test_wrong_world_review_refuses_before_writing_any_object(self):
        self.fixture()
        self.distribution["plan"] = {}
        self.review["judgment"]["world"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory, self.patches(), self.assertRaises(Refusal):
            semantics.issue(self.held(directory))
        self.assertFalse(self.store.values)

    def test_failed_shared_materials_cannot_create_an_issued_record(self):
        self.fixture()
        self.distribution["plan"] = {}
        protocol = self.protocol
        def unknown(arguments):
            return (3, {"state": "unknown", "publication": False, "reason": "review permission unavailable"}) if arguments[:2] == ["release", "inspect"] else protocol(arguments)
        with tempfile.TemporaryDirectory() as directory, self.patches(), mock.patch.object(semantics.reader, "capture", side_effect=unknown), self.assertRaisesRegex(Refusal, "inspect returned unknown.*review permission unavailable"):
            semantics.issue(self.held(directory))
        self.assertEqual(len(self.store.values), 1)

    def test_reader_failure_retains_bounded_cause_without_accepting_invalid_reports(self):
        for state, status in (("unknown", 3), ("refused", 1)):
            report = {"state": state, "publication": False, "reason": "native permission unavailable"}
            with self.subTest(state=state), mock.patch.object(semantics.reader, "capture", return_value=(status, report)), self.assertRaisesRegex(Refusal, f"review returned {state}.*native permission unavailable"):
                semantics.facts(["release", "review"])
        for changes in ({"reason": None}, {"reason": ""}, {"reason": "x" * 4097}, {"publication": True}, {"state": "observed"}):
            report = {"state": "unknown", "publication": False, "reason": "bounded"} | changes
            with self.subTest(changes=tuple(changes)), mock.patch.object(semantics.reader, "capture", return_value=(3, report)), self.assertRaises(Refusal):
                semantics.facts(["release", "review"])

    def test_same_address_requires_identical_bytes_and_unknown_writes_never_replay(self):
        self.fixture()
        raw = canonical.encode(self.world)
        with mock.patch.object(semantics.release, "fetch", side_effect=self.store.public):
            reference = semantics.retain(self.store, "plumb", raw)
            self.assertEqual(semantics.retain(self.store, "plumb", raw), reference)
            key = next(iter(self.store.values))
            self.store.values[key] = b"foreign"
            with self.assertRaises(Refusal):
                semantics.retain(self.store, "plumb", raw)
        broken = mock.Mock()
        broken.snapshot.return_value = (None, None)
        broken.create.side_effect = OSError("unknown accepted state")
        with self.assertRaises(OSError):
            semantics.retain(broken, "plumb", raw)
        broken.create.assert_called_once()
        broken.get.assert_not_called()

    def test_material_facts_cannot_claim_authentication_or_publication(self):
        for field in ("authentication", "publication"):
            self.fixture()
            self.distribution["plan"] = {}
            protocol = self.protocol
            def excessive(arguments):
                status, report = protocol(arguments)
                if arguments[:2] == ["release", "inspect"]:
                    report["facts"][field] = True
                return status, report
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, self.patches(), mock.patch.object(semantics.reader, "capture", side_effect=excessive), self.assertRaises(Refusal):
                semantics.issue(self.held(directory))
            self.assertEqual(len(self.store.values), 1)

    def test_confirmation_refuses_tamper_and_moved_materials(self):
        self.fixture()
        self.distribution["plan"] = {}
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            issued = semantics.issue(held)
            digest = issued["reference"]["sha256"]
            with self.assertRaises(Refusal):
                semantics.confirm(held | {"digest": "0" * 64})
            prior = copy.deepcopy(self.scenes)
            self.scenes["fixture"] = "moved-current-scene"
            with self.assertRaises(Refusal):
                semantics.confirm(held | {"digest": digest})
            self.scenes.clear()
            self.scenes.update(prior)
            with mock.patch.object(semantics.reader, "identity", return_value={}):
                with self.assertRaises(Refusal):
                    semantics.confirm(held | {"digest": digest})


class History(unittest.TestCase):
    def test_changed_shared_preparation_cannot_write_a_checkpoint_or_head(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        calls = []
        def moving(arguments):
            status, report = fixture.protocol(arguments)
            if "retention" in arguments:
                calls.append(arguments)
                if len(calls) > 1:
                    report["facts"]["data"]["sequence"] = 2
            return status, report
        with tempfile.TemporaryDirectory() as directory, fixture.patches(), mock.patch.object(semantics.reader, "capture", side_effect=moving):
            with self.assertRaises(Refusal):
                semantics.checkpoint(fixture.held(directory))
            fixture.store.create.assert_not_called()
            fixture.store.swap.assert_not_called()

    def test_exact_checkpoint_and_head_are_retained_before_native_confirmation(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        with tempfile.TemporaryDirectory() as directory, fixture.patches():
            held = fixture.held(directory)
            intent = semantics.checkpoint(held)
            fixture.store.swap.assert_called_once_with(semantics.HEAD, canonical.encode(intent["head"]), None)
            self.assertEqual(len(fixture.values), 2)
            self.assertFalse(intent["authentication"] or intent["publication"])
            confirmed = semantics.confirmed(held | {"digest": intent["reference"]["sha256"]})
            self.assertEqual(confirmed["reference"], intent["reference"])
            semantics.r2.writer.assert_called_once()

    def test_unknown_shared_preparation_cannot_initialize_a_writer(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        def unknown(arguments):
            if "retention" in arguments:
                return 3, {"state": "unknown", "reason": "earlier checkpoint unavailable", "publication": False}
            return fixture.protocol(arguments)
        with tempfile.TemporaryDirectory() as directory, fixture.patches(), mock.patch.object(semantics.reader, "capture", side_effect=unknown):
            held = fixture.held(directory)
            with self.assertRaisesRegex(Refusal, "earlier checkpoint unavailable"):
                semantics.checkpoint(held)
            semantics.r2.writer.assert_not_called()
            self.assertFalse((Path(held["source"]) / "intent.json").exists())

    def test_wrong_native_or_excessive_preparer_authority_is_refused_before_writer(self):
        for changed in ("authentication", "native"):
            fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
            def invalid(arguments):
                status, report = fixture.protocol(arguments)
                if "retention" in arguments:
                    if changed == "authentication":
                        report["facts"][changed] = True
                    else:
                        report["facts"]["data"]["native"] = {}
                return status, report
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory, fixture.patches(), mock.patch.object(semantics.reader, "capture", side_effect=invalid):
                with self.assertRaises(Refusal):
                    semantics.checkpoint(fixture.held(directory))
                semantics.r2.writer.assert_not_called()

    def test_public_private_disagreement_and_cas_conflict_never_overwrite_a_head(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        fixture.values[semantics.HEAD] = b"foreign"
        with tempfile.TemporaryDirectory() as directory, fixture.patches(), self.assertRaises(Refusal):
            semantics.checkpoint(fixture.held(directory))
        fixture.store.create.assert_not_called()
        fixture.store.swap.assert_not_called()
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        fixture.store.swap.side_effect = Conflict("competing version")
        with tempfile.TemporaryDirectory() as directory, fixture.patches(), self.assertRaises(Conflict):
            semantics.checkpoint(fixture.held(directory))
        fixture.store.swap.assert_called_once()

    def test_unknown_applied_write_keeps_original_intent_and_never_replays(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        def uncertain(key, raw, etag):
            fixture.swap(key, raw, etag)
            raise OSError("unknown head acknowledgement")
        fixture.store.swap.side_effect = uncertain
        with tempfile.TemporaryDirectory() as directory, fixture.patches():
            held = fixture.held(directory)
            with self.assertRaises(OSError):
                semantics.checkpoint(held)
            self.assertTrue((Path(held["source"]) / "intent.json").is_file())
            self.assertIn(semantics.HEAD, fixture.values)
            with self.assertRaises(Refusal):
                semantics.checkpoint(held)
            fixture.store.swap.assert_called_once()

    def test_moved_public_head_cannot_receive_a_digest_confirmation(self):
        fixture = Ledger(SOURCE, NATIVE, AUTHORITY)
        with tempfile.TemporaryDirectory() as directory, fixture.patches():
            held = fixture.held(directory)
            intent = semantics.checkpoint(held)
            fixture.values[semantics.HEAD] = b"moved"
            with self.assertRaises(Refusal):
                semantics.confirmed(held | {"digest": intent["reference"]["sha256"]})
