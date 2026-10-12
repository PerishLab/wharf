import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import apply
from lib.refusal import Refusal
from lib.store.lane import public, publication, registration, state
from tests.cfworker import test_readback
from tests.lib.identity import test_publication
from tests.lib.preview.test_store import Store
from tests.lib.store.memory import Memory


class Outcome(unittest.TestCase):
    def setUp(self):
        self.fixture = test_readback.Readback()
        self.fixture.setUp()
        self.plan, self.after = self.fixture.plan, self.fixture.after
        self.proof = self.fixture.invoke(self.fixture.client())
        self.registered = self.plan["apply"]["registration"]
        self.declared = self.registered["declaration"]
        self.active = self.plan["apply"]["current"]["active"]

    def settled(self, plan=None):
        return apply.settle(self.plan if plan is None else plan, self.after, self.proof, "idle")

    def recorded(self, operation):
        return codec.read("record", {"schema": 2, "entry": {"kind": "operation", "value": operation}})

    def test_exact_readback_and_idle_writer_bind_shared_operation(self):
        operation = self.settled()
        self.assertEqual({key: operation[key] for key in ("request", "assessment")}, self.active)
        self.assertEqual(operation["outcome"], {"outcome": "applied", "material": {"state": "present", "artifact": self.plan["apply"]["artifact"]}, "writer": "idle"})
        self.assertEqual(codec.load("record", codec.encode("record", self.recorded(operation))), self.recorded(operation))
        settled = state.finished(self.plan["apply"]["current"], operation, self.registered)
        self.assertEqual(settled["revision"], self.plan["apply"]["current"]["revision"] + 1)
        self.assertEqual(settled["observed"]["digest"], self.proof["artifact"]["digest"])

    def test_active_unknown_boolean_or_timeout_writer_cannot_settle(self):
        for writer in ("active", "unknown", None, True, False, 0, "expired", {"idle_after": 300}):
            with self.assertRaises(Refusal):
                apply.settle(self.plan, self.after, self.proof, writer)

    def test_wrong_readback_request_artifact_identity_address_or_files_refuse(self):
        for field, value in (("request", "0" * 64), ("artifact", dict(self.proof["artifact"], digest="0" * 64)),
                             ("deployment", dict(self.proof["deployment"], id="latest")),
                             ("deployment", dict(self.proof["deployment"], id=self.fixture.fixture.projected(2)["id"])),
                             ("address", "https://preview-crest-development.test.workers.dev"),
                             ("address", self.proof["address"] + "/"), ("files", True), ("files", 1), ("files", 1002), ("files", 2.0)):
            proof = dict(self.proof, **{field: value})
            with self.assertRaises(Refusal):
                apply.settle(self.plan, self.after, proof, "idle")
        for value in ({}, dict(self.proof, token="private"), dict(self.proof, deadline=0)):
            with self.assertRaises(Refusal):
                apply.settle(self.plan, self.after, value, "idle")

    def test_parent_sibling_preview_or_retained_deployment_drift_refuses(self):
        for observed in (self.plan["before"], dict(self.after, deployments=[]),
                         dict(self.after, previews=[self.after["preview"]]), dict(self.after, preview=None),
                         dict(self.after, parent=dict(self.after["parent"], id="foreign"))):
            with self.assertRaises(Refusal):
                apply.settle(self.plan, observed, self.proof, "idle")

    def test_unknown_result_retains_reservation_and_never_fabricates_applied_material(self):
        for reason in ("unavailable", "unverified"):
            operation = apply.unknown(self.plan, reason)
            current = state.finished(self.plan["apply"]["current"], operation, self.registered)
            self.assertEqual(current["phase"], "unknown")
            self.assertEqual(current["revision"], self.plan["apply"]["current"]["revision"])
            self.assertEqual(current["active"], self.active)
            self.assertEqual(current["observed"], self.plan["apply"]["current"]["observed"])
            self.assertEqual(operation["outcome"], {"outcome": "unknown", "reason": reason})
        for reason in ("denied", "conflict", "timeout", None, {}):
            with self.assertRaises(Refusal):
                apply.unknown(self.plan, reason)

    def test_only_exact_unknown_request_with_readback_and_idle_writer_can_recover(self):
        unknown = apply.unknown(self.plan, "unavailable")
        current = state.finished(self.plan["apply"]["current"], unknown, self.registered)
        plan = apply.resume(self.plan, current)
        operation = self.settled(plan)
        self.assertEqual(operation, self.settled())
        self.assertEqual(state.finished(current, operation, self.registered)["phase"], "settled")

    def test_publication_and_public_settlement_reuse_existing_shared_records(self):
        operation = self.settled()
        record = self.recorded(operation)
        body = codec.encode("record", record)
        response = test_publication.Response(body)
        connection = test_publication.Connection(response)
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        writer = Memory()
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(writer, reader, self.declared, record)
            self.assertEqual(apply.published(self.plan, reader, reference), operation)
            count = len(writer.writes)
            self.assertEqual(publication.publish(writer, reader, self.declared, record), reference)
            self.assertEqual(len(writer.writes), count)
        self.assertEqual(reference, reader.reference(codec.digest("record", record)))
        self.assertTrue(all(path == "/" + public.location(reference) for _, path, _ in connection.calls))
        self.assertEqual(len(connection.calls), 3)
        self.assertNotIn("deployment", body.decode())

    def test_unavailable_public_evidence_cannot_settle_or_advance_state(self):
        operation = self.settled()
        body = codec.encode("record", self.recorded(operation))
        response = test_publication.Response(b"unavailable", 404)
        connection = test_publication.Connection(response)
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        writer = Memory()
        bucket = Store()
        registration.write(bucket, self.registered, None)
        current = state.reserve(bucket, self.active["request"], self.active["assessment"])
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(Refusal):
                publication.publish(writer, reader, self.declared, self.recorded(operation))
            reference = reader.reference(codec.digest("record", self.recorded(operation)))
            with self.assertRaises(Refusal):
                apply.published(self.plan, reader, reference)
        self.assertEqual(writer.get(public.location(reference)), body)
        self.assertEqual(state.read(bucket, self.registered)[0], current)

    def test_unknown_records_publish_but_never_publicly_settle(self):
        for reason in ("unavailable", "unverified"):
            operation = apply.unknown(self.plan, reason)
            record = self.recorded(operation)
            connection = test_publication.Connection(test_publication.Response(codec.encode("record", record)))
            reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
            with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
                reference = publication.publish(Memory(), reader, self.declared, record)
                with self.assertRaises(Refusal):
                    apply.published(self.plan, reader, reference)

    def test_public_evidence_keeps_exact_intent_precondition_and_declaration_authority(self):
        operation = self.settled()
        record = self.recorded(operation)
        connection = test_publication.Connection(test_publication.Response(codec.encode("record", record)))
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(Memory(), reader, self.declared, record)
            for field in ("intent", "expected"):
                requested = copy.deepcopy(operation["request"])
                if field == "intent":
                    requested[field]["artifact"]["digest"] = "0" * 64
                else:
                    requested[field] = {"state": "unknown"}
                with self.assertRaises(Refusal):
                    publication.settle(reader, self.declared, reference, requested)
            declared = copy.deepcopy(self.declared)
            declared["authorities"]["publication"] = "other"
            with self.assertRaises(Refusal):
                publication.publish(Memory(), reader, declared, record)

    def test_operation_and_content_projections_are_detached(self):
        held = apply.readback(self.plan, self.after, self.proof)
        held["artifact"]["digest"] = "0" * 64
        operation = self.settled()
        operation["request"]["context"]["request"] = "0" * 64
        self.assertEqual(self.settled()["request"], self.active["request"])

    def test_independent_public_readback_precedes_state_record_of_exact_operation(self):
        operation = self.settled()
        record = self.recorded(operation)
        connection = test_publication.Connection(test_publication.Response(codec.encode("record", record)))
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        bucket = Store()
        registration.write(bucket, self.registered, None)
        current = state.reserve(bucket, self.active["request"], self.active["assessment"])
        unknown = state.record(bucket, apply.unknown(self.plan, "unavailable"))
        plan = apply.resume(self.plan, unknown)
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(Memory(), reader, self.declared, record)
            verified = apply.published(plan, reader, reference)
            settled = state.record(bucket, verified)
        self.assertEqual(settled["revision"], current["revision"] + 1)
        self.assertEqual(settled["last"], operation)
        self.assertEqual(settled["phase"], "settled")
        self.assertEqual(len(connection.calls), 2)

    def test_matching_public_request_cannot_replace_original_assessment(self):
        operation = self.settled()
        operation["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
        record = self.recorded(operation)
        connection = test_publication.Connection(test_publication.Response(codec.encode("record", record)))
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(Memory(), reader, self.declared, record)
            self.assertEqual(publication.settle(reader, self.declared, reference, operation["request"]), operation)
            with self.assertRaisesRegex(Refusal, "original reservation assessment"):
                apply.published(self.plan, reader, reference)
