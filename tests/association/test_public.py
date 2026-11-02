import contextlib
import copy
import http.client
import io
import ssl
import tempfile
import urllib.error
import json
import unittest
from unittest import mock
from pathlib import Path

from scripts import distribution

from lib.content import canonical
from lib.identity.bind import producer as linux
from lib.process.association import producer, public, spec
from lib.refusal import Refusal
from tests.association.fixtures import MACOS, WINDOWS
from tests.association.world import AUTHORITY, World


class Public(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def publish(self):
        with self.world.wired():
            return producer.produce(self.world.held)

    def test_real_native_plan_and_complete_records_publish_both_targets(self):
        before = copy.deepcopy(self.world.documents)
        result = self.publish()
        self.assertEqual(set(result["proofs"]), {MACOS, WINDOWS})
        self.assertFalse(result["promotion"])
        with self.world.wired():
            for selected in (MACOS, WINDOWS):
                proof, normalized = public.read("PerishLab/demo", self.world.source["marker"], selected)
                self.assertEqual(set(normalized), {"demo"})
                self.assertEqual(set(proof["binaries"]), {"demo", "demo-api"})
                self.assertEqual(proof["managers"]["canonical"]["state"], "not-published")
        self.assertEqual(before, self.world.documents)
        self.assertFalse(any(key.endswith(("seal.json", "distribution.json")) for key in self.world.public.objects))

    def test_published_and_installed_manager_checks_are_independent(self):
        self.publish()
        location = self.world.seal["managers"]["windows"]["url"]
        self.world.documents[location] += b"changed"
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], WINDOWS)

    def test_duplicate_unknown_or_downgraded_proof_fields_refuse(self):
        result = self.publish()
        key = result["proofs"][MACOS]["url"].removeprefix(AUTHORITY + "/")
        original = self.world.public.get(key)
        for raw in (b'{"schema":1,"schema":2}', canonical.encode(dict(json.loads(original), unknown=True)), canonical.encode(dict(json.loads(original), schema="wharf.binary.association/v1"))):
            with self.subTest(raw=raw[:40]), self.world.wired(), mock.patch.object(public, "reference", return_value=raw), self.assertRaises(Refusal):
                public.read("PerishLab/demo", self.world.source["marker"], MACOS)

    def test_current_discovery_and_foreign_objects_do_not_fall_back(self):
        result = self.publish()
        location = self.world.root + "/build/native-darwin-arm64.json"
        original = self.world.public.get(location)
        index = json.loads(original)
        index["proof"]["url"] = "https://foreign.example/" + index["proof"]["name"]
        self.world.public.objects[location] = canonical.encode(index)
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], MACOS)
        self.world.public.objects[location] = original
        self.world.distribution["resolution"]["binaries"]["tree"] = "f" * 40
        self.world.documents[AUTHORITY + "/" + self.world.root + "/distribution.json"] = canonical.encode(self.world.distribution)
        with self.world.wired(), self.assertRaises(Refusal):
            public.read("PerishLab/demo", self.world.source["marker"], MACOS)

    def test_wrong_role_refuses_before_workload_or_release_write(self):
        self.world.role = "build-proof"
        before = copy.deepcopy(self.world.blob.objects)
        with self.world.wired(), self.assertRaises(Refusal):
            producer.produce(self.world.held)
        self.assertEqual(before, self.world.blob.objects)
        self.assertFalse(self.world.public.objects)

    def test_original_linux_proof_reader_stays_unchanged(self):
        self.world.role = "build-proof"
        with self.world.wired():
            result = linux.produce(self.world.held)
            self.assertEqual(result["state"], "proven")
        self.assertFalse(any("native-" in key for key in self.world.public.objects))


class Transport(unittest.TestCase):
    def test_network_failures_are_bounded_safe_and_never_retried(self):
        cases = [(TimeoutError("SECRET"), "timeout"), (urllib.error.URLError(ssl.SSLError("SECRET")), "tls"), (urllib.error.HTTPError("https://SECRET", 503, "SECRET", {}, None), "http"), (http.client.IncompleteRead(b"SECRET"), "read"), (urllib.error.URLError("SECRET"), "read")]
        for failure, kind in cases:
            with self.subTest(kind=kind), mock.patch.object(spec.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(spec.Unavailable) as raised:
                    spec.fetch("https://example.test/object")
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("SECRET", json.dumps(raised.exception.details))
                self.assertEqual(raised.exception.details["status"], 503 if kind == "http" else None)
                build.return_value.open.assert_called_once()
                self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 30})
                self.assertEqual(build.call_args.args[0].proxies, {})

    def test_transform_bounds_redirects_and_content_disagreement_stay_refused(self):
        with mock.patch.object(spec.urllib.request, "build_opener") as build:
            reply = build.return_value.open.return_value.__enter__.return_value
            reply.status = 200
            reply.headers = {"Content-Encoding": "identity"}
            reply.read.return_value = b"valid"
            self.assertEqual(spec.fetch("https://example.test/object", 5), b"valid")
            reply.read.assert_called_once_with(6)
            for body, encoding in ((b"too large", "identity"), (b"", "identity"), (b"valid", "gzip")):
                reply.read.return_value, reply.headers = body, {"Content-Encoding": encoding}
                with self.assertRaises(Refusal) as raised:
                    spec.fetch("https://example.test/object", 5)
                self.assertNotIsInstance(raised.exception, spec.Unavailable)
        with self.assertRaises(Refusal):
            spec.Redirect().redirect_request(None)
        value = {"url": AUTHORITY + "/v1/objects/sha256/" + "a" * 64 + "/demo.zip", "name": "demo.zip", "mime": "application/zip", "size": 5, "sha256": "a" * 64}
        with mock.patch.object(public, "fetch", return_value=b"valid"), self.assertRaises(Refusal) as raised:
            public.reference(value, AUTHORITY, "archive")
        self.assertNotIsInstance(raised.exception, spec.Unavailable)

    def test_real_producer_context_is_retained_without_a_ready_or_proof(self):
        world = World()
        original = world.fetch

        def fetch(url):
            if url == world.seal["artifacts"]["darwin-arm64"]["url"]:
                raise spec.Unavailable("timeout")
            return original(url)

        with world.wired(), mock.patch.object(world, "fetch", side_effect=fetch), mock.patch.object(producer.parameters, "answer") as answer:
            with self.assertRaises(spec.Unavailable) as raised:
                producer.produce(world.held)
            answer.assert_not_called()
        self.assertEqual(raised.exception.details, {"stage": "archive", "target": MACOS, "marker": world.source["marker"], "status": None})
        self.assertFalse(world.public.objects)

    def test_partial_producer_failure_does_not_claim_complete_coverage(self):
        world = World()
        original = world.fetch

        def fetch(url):
            if url == world.seal["artifacts"]["windows-x64"]["url"]:
                raise spec.Unavailable("tls")
            return original(url)

        with world.wired(), mock.patch.object(world, "fetch", side_effect=fetch), mock.patch.object(producer.parameters, "answer") as answer:
            with self.assertRaises(spec.Unavailable) as raised:
                producer.produce(world.held)
            answer.assert_not_called()
        self.assertEqual(raised.exception.details["target"], WINDOWS)
        self.assertTrue(any(key.endswith("native-darwin-arm64.json") for key in world.public.objects))
        self.assertFalse(any(key.endswith("native-windows-x64.json") for key in world.public.objects))

    def test_public_reader_retains_known_identity_and_stage(self):
        with mock.patch.object(public, "fetch", side_effect=spec.Unavailable("http", {"status": 502})), self.assertRaises(spec.Unavailable) as raised:
            public.read("PerishLab/demo", "v1.2.3", WINDOWS)
        self.assertEqual(raised.exception.details, {"stage": "discovery", "target": WINDOWS, "marker": "v1.2.3", "status": 502})
        with mock.patch.object(public.managers, "verify", side_effect=spec.Unavailable("read")), self.assertRaises(spec.Unavailable) as raised:
            public.managed({}, {})
        self.assertEqual(raised.exception.details["stage"], "manager")


class Observation(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.held = {"repository": "PerishLab/demo", "marker": "v1.2.3", "observation": str(self.root)}

    def test_cli_failure_retains_unknown_bounded_record_and_nonzero_exit(self):
        output = io.StringIO()
        handler = mock.Mock(side_effect=spec.Unavailable("timeout", {"stage": "archive", "target": WINDOWS}))
        with mock.patch.dict(distribution.ACTIONS, {"native-read": (handler, ["repository", "marker", "observation"])}), contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            result = distribution.main(["native-read", "--repository", "PerishLab/demo", "--marker", "v1.2.3", "--observation", str(self.root)])
        self.assertEqual(result, 2)
        raw = (self.root / "native-read.json").read_bytes()
        self.assertLessEqual(len(raw), 4096)
        record = json.loads(raw)
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["failure"]["marker"], "v1.2.3")
        self.assertEqual(record["failure"]["target"], WINDOWS)
        self.assertIsNone(record["associated"])
        self.assertFalse(record["promotion"])
        self.assertNotIn("Traceback", output.getvalue())
        handler.assert_called_once()

    def test_records_never_authorize_skip_or_overwrite_a_different_result(self):
        handler = mock.Mock(return_value={"associated": True})
        args = ["native-read", "--repository", "PerishLab/demo", "--marker", "v1.2.3", "--observation", str(self.root)]
        with mock.patch.dict(distribution.ACTIONS, {"native-read": (handler, ["repository", "marker", "observation"])}), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(distribution.main(args), 0)
            original = (self.root / "native-read.json").read_bytes()
            self.assertEqual(distribution.main(args), 0)
            handler.side_effect = spec.Unavailable("read")
            self.assertEqual(distribution.main(args), 2)
        self.assertEqual(handler.call_count, 3)
        self.assertEqual((self.root / "native-read.json").read_bytes(), original)

    def test_refusal_and_missing_context_never_echo_unvalidated_values(self):
        held = dict(self.held, repository="SECRET", marker="SECRET", reference="SECRET", run="SECRET", observer="SECRET")
        public.observe("native-compare", held, Refusal("SECRET"))
        raw = (self.root / "native-compare.json").read_bytes()
        self.assertNotIn(b"SECRET", raw)
        result = json.loads(raw)
        self.assertEqual(result["state"], "refused")
        self.assertTrue(all(value is None for value in result["request"].values()))
        self.assertIsNone(result["associated"])
        with mock.patch.object(public, "Path", side_effect=AssertionError("write")):
            public.observe("native-read", {}, spec.Unavailable("tls"))

    def test_writing_failure_keeps_the_action_failed(self):
        held = dict(self.held, observation=str(self.root / "file" / "nested"))
        (self.root / "file").write_text("occupied")
        with self.assertRaisesRegex(Refusal, "could not be retained"):
            public.observe("native-read", held, {})
