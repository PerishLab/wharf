import copy
import io
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind.spec import entry
from lib.process.confirmation import retention
from lib.refusal import Refusal
from tests.confirmation import test_world
from tests.promotion.preparation.test_capture import SOURCE

CURRENT = {"run": 10, "attempt": 1, "commit": "c" * 40, "job": 29}


class Retention(unittest.TestCase):
    def fixture(self):
        producer = test_world.World()
        producer.fixture()
        with tempfile.TemporaryDirectory() as directory, producer.patches():
            held = producer.held(directory)
            test_world.world.source(held)
            result = test_world.world.produce(held)
            test_world.world.confirm(held | {"observation": canonical.encode({"world": result["world"]["sha256"], "source": result["material"]["sha256"]}).decode()})
            self.files = {path.name: path.read_bytes() for path in Path(held["artifact"]).iterdir()}
        self.native = test_world.NATIVE
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "retain", "native": self.native, "world": entry(self.files["world.json"])["sha256"], "material": entry(self.files["source.json"])["sha256"]}
        self.packed = self.pack(self.files)
        repository = {"id": 1374358737, "full_name": "PerishLab/wharf"}
        self.run = {"id": 9, "run_attempt": 1, "head_sha": self.native["commit"], "head_branch": "main", "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "repository": repository, "head_repository": repository, "status": "completed"}
        self.jobs = {"total_count": 1, "jobs": [{"id": 19, "run_id": 9, "run_attempt": 1, "head_sha": self.native["commit"], "head_branch": "main", "name": "[prepare] semantic world", "status": "completed", "conclusion": "success", "steps": [{"name": f"[prepare] world {self.request['world']} {self.request['material']}", "status": "completed", "conclusion": "success"}]}]}
        self.artifact = {"id": 11, "name": "semantics-world-1", "size_in_bytes": len(self.packed), "digest": "sha256:" + entry(self.packed)["sha256"], "expired": False, "workflow_run": {"id": 9, "repository_id": 1374358737, "head_repository_id": 1374358737, "head_branch": "main", "head_sha": self.native["commit"]}}
        self.catalog = {"total_count": 1, "artifacts": [self.artifact]}
        self.actions = mock.Mock()
        self.actions.get.side_effect = self.api
        self.actions.archive.side_effect = lambda identity: self.packed
        self.public = {}
        self.authority = producer.authority

    def pack(self, files):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
        return stream.getvalue()

    def api(self, route):
        if route == "/actions/runs/9/attempts/1":
            return copy.deepcopy(self.run)
        if route == "/actions/runs/9/attempts/1/jobs?per_page=100":
            return copy.deepcopy(self.jobs)
        if route == "/actions/runs/9/artifacts?per_page=100":
            return copy.deepcopy(self.catalog)
        raise AssertionError(route)

    def store(self, writer, product, raw):
        reference = retention.semantics.object_reference(product, raw)
        self.public[reference["url"]] = raw
        return reference

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (retention.capture, "active", {"return_value": CURRENT}),
            (retention.capture, "lineage", {}),
            (retention.preflight, "event", {"return_value": {"semantics": canonical.encode(self.request).decode()}}),
            (retention.pull, "Actions", {"return_value": self.actions}),
            (retention.capture, "inspect", {"side_effect": lambda value, raw: (self.authority, {"state": "observed", "data": value, "authentication": False, "publication": False})}),
            (retention.reader, "identity", {"return_value": self.authority}),
            (retention.release, "place", {"return_value": (None, "pilot-bucket", None)}),
            (retention.r2, "writer", {}),
            (retention.semantics, "retain", {"side_effect": self.store}),
            (retention.release, "fetch", {"side_effect": self.public.__getitem__}),
            (retention.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def held(self, directory):
        return {"observer": {}, "repository": SOURCE["repository"], "marker": SOURCE["marker"], "artifact": str(Path(directory) / "retained")}

    def test_original_native_bytes_are_retained_and_confirmed_without_product_execution(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            intent = retention.produce(held)
            record = intent["preparation"]
            self.assertEqual(record["native"], self.native)
            self.assertNotEqual(record["native"], intent["native"])
            self.assertEqual(len(self.public), 3)
            for field, name in (("world", "world.json"), ("material", "source.json")):
                self.assertEqual(self.public[record[field]["url"]], self.files[name])
            retention.r2.writer.assert_called_once_with("pilot-bucket", "RELEASES")
            retention.r2.writer.reset_mock()
            confirmed = retention.confirm(held | {"digest": intent["reference"]["sha256"]})
            retention.r2.writer.assert_not_called()
            self.assertFalse(any(confirmed[name] for name in ("authentication", "qualification", "activation", "publication")))

    def test_failed_wrong_or_incomplete_original_job_cannot_initialize_a_writer(self):
        for field, value in [("conclusion", "failure"), ("id", 20), ("run_attempt", 2), ("head_branch", "feature"), ("name", "[issue] version semantics"), ("steps", [])]:
            self.fixture()
            self.jobs["jobs"][0][field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, self.patches():
                with self.assertRaises(Refusal):
                    retention.produce(self.held(directory))
                retention.r2.writer.assert_not_called()

    def test_wrong_digest_and_ambiguous_job_confirmation_refuse(self):
        for changed in ("world", "material", "duplicate"):
            self.fixture()
            if changed == "duplicate":
                self.jobs["jobs"][0]["steps"] *= 2
            else:
                self.request[changed] = "0" * 64
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory, self.patches():
                with self.assertRaises(Refusal):
                    retention.produce(self.held(directory))
                retention.r2.writer.assert_not_called()

    def test_missing_expired_foreign_or_changed_artifact_cannot_write(self):
        for field, value in [("expired", True), ("digest", "sha256:" + "0" * 64), ("size_in_bytes", 1), ("workflow_run", {}), ("name", "other-attempt")]:
            self.fixture()
            self.artifact[field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory, self.patches():
                with self.assertRaises(Refusal):
                    retention.produce(self.held(directory))
                retention.r2.writer.assert_not_called()
        self.fixture()
        self.catalog["total_count"] = 2
        with tempfile.TemporaryDirectory() as directory, self.patches():
            with self.assertRaises(Refusal):
                retention.produce(self.held(directory))
            retention.r2.writer.assert_not_called()

    def test_archive_paths_links_duplicate_members_and_oversized_files_refuse(self):
        self.fixture()
        changed = self.files | {"../escape": b"foreign"}
        with self.assertRaises(Refusal):
            retention.unpack(self.pack(changed))
        duplicate = io.BytesIO(self.pack(self.files))
        with zipfile.ZipFile(duplicate, "a") as archive, self.assertWarns(UserWarning):
            archive.writestr("source.json", b"duplicate")
        with self.assertRaises(Refusal):
            retention.unpack(duplicate.getvalue())
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, raw in self.files.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o120777 if name == "source.json" else 0o100644) << 16
                archive.writestr(info, raw)
        with self.assertRaises(Refusal):
            retention.unpack(stream.getvalue())
        with self.assertRaises(Refusal):
            retention.unpack(self.pack(self.files | {"source.json": b"x" * (retention.capture.LIMIT + 1)}))

    def test_native_confirmed_bytes_cannot_be_replaced_inside_a_valid_archive(self):
        self.fixture()
        changed = self.files | {"source.json": b"{}"}
        with self.assertRaises(Refusal):
            retention.files(self.pack(changed), self.request)
        selected = retention.capture.decode(self.files["selection.json"])
        selected["native"] = CURRENT
        with self.assertRaises(Refusal):
            retention.files(self.pack(self.files | {"selection.json": canonical.encode(selected)}), self.request)

    def test_transport_unknown_preserves_refusal_before_writer_and_unknown_write_keeps_intent(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            self.actions.archive.side_effect = OSError("observation unavailable")
            with self.assertRaisesRegex(Refusal, "unreadable"):
                retention.produce(held)
            retention.r2.writer.assert_not_called()
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            retention.semantics.retain.side_effect = OSError("unknown create outcome")
            with self.assertRaises(OSError):
                retention.produce(held)
            root = Path(held["artifact"])
            self.assertTrue((root / "intent.json").is_file())
            self.assertFalse((root / "reference.json").exists())

    def test_wrong_product_marker_or_changed_public_bytes_refuse(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            held = self.held(directory)
            with self.assertRaises(Refusal):
                retention.produce(held | {"repository": "PerishLab/concord"})
            retention.r2.writer.assert_not_called()
            intent = retention.produce(held)
            self.public[intent["preparation"]["material"]["url"]] = b"foreign"
            with self.assertRaises(Refusal):
                retention.confirm(held | {"digest": intent["reference"]["sha256"]})

    def test_changed_original_facts_and_unknown_shared_reader_refuse_before_writer(self):
        self.fixture()
        first = copy.deepcopy(self.jobs)
        second = copy.deepcopy(self.jobs)
        second["jobs"][0]["steps"][0]["name"] = "changed"
        original = self.api
        replies = iter([first, second])
        self.actions.get.side_effect = lambda route: next(replies) if route.endswith("/jobs?per_page=100") else original(route)
        with tempfile.TemporaryDirectory() as directory, self.patches():
            with self.assertRaises(Refusal):
                retention.produce(self.held(directory))
            retention.r2.writer.assert_not_called()
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, self.patches():
            retention.capture.inspect.side_effect = Refusal("shared reader returned unknown")
            with self.assertRaisesRegex(Refusal, "unknown"):
                retention.produce(self.held(directory))
            retention.r2.writer.assert_not_called()
