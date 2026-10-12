import copy
import json
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import apply, create, discard, payload, reader, upload, writer
from lib.refusal import Refusal
from lib.store.lane import public, publication, registration, state
from tests.cfworker import test_inspection
from tests.lib.identity import test_publication
from tests.lib.preview.test_store import Store
from tests.lib.process import test_cfworker_upload
from tests.lib.process.test_cfworker import Response
from tests.lib.store.memory import Memory


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.fixture = test_inspection.Inspection()
        self.fixture.setUp()
        self.deployment = self.fixture.fixture.fixture
        self.content = self.deployment.fixture
        self.body, self.files = self.content.document, self.content.bodies
        self.store, self.evidence = Store(), Memory()
        self.public = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        self.response = test_publication.Response(b"")
        self.connection = test_publication.Connection(self.response)
        self.network = mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=self.connection)
        self.network.start()
        self.addCleanup(self.network.stop)

    def publish(self, operation, declared):
        record = {"schema": 2, "entry": {"kind": "operation", "value": operation}}
        self.response.body = codec.encode("record", record)
        return publication.publish(self.evidence, self.public, declared, record)

    def reply(self, api, value, status=200):
        body = json.dumps({"success": True, "errors": [], "result": value}).encode()
        if not isinstance(api._client, mock.Mock):
            api._client = mock.Mock()
        def received(request, timeout):
            response = Response(body, request.full_url)
            response.status = status
            return response
        api._client.open.side_effect = received

    def deploy(self, lane):
        held = self.content.prepared(lane)
        registered = held["registration"]
        registration.write(self.store, registered, None)
        active = held["current"]["active"]
        held["current"] = state.reserve(self.store, active["request"], active["assessment"])
        self.deployment.preview["name"] = lane.replace(".", "-")
        before = copy.deepcopy(self.deployment.before)
        absent = {"parent": before["parent"], "preview": None, "previews": [before["previews"][1]]}
        creation = create.prepare(held, absent, self.deployment.policy)
        creator = create.Creator("private-create", creation)
        self.reply(creator, {"id": before["preview"]["id"], "name": before["preview"]["name"], "slug": before["preview"]["name"]})
        created = creator.create(absent, self.body, self.files.__getitem__)
        create.confirmed(creation, {key: before[key] for key in ("parent", "previews", "preview")}, created["preview"])
        uploader = upload.Uploader("private-session", held)
        hashes = list(dict.fromkeys(item["hash"] for item in payload.manifest(held, self.body, self.files.__getitem__).values()))
        self.reply(uploader, {"jwt": test_cfworker_upload.JWT, "buckets": [hashes]})
        self.assertTrue(uploader.session(self.body, self.files.__getitem__)["acknowledged"])
        self.reply(uploader, {"jwt": test_cfworker_upload.FINAL}, 201)
        self.assertTrue(uploader.bucket(0, self.body, self.files.__getitem__)["complete"])
        completion = uploader.completion(self.body, self.files.__getitem__)
        plan = apply.deployment_prepare(held, before, self.deployment.policy)
        deployer = writer.Deployer("private-deploy", plan, completion)
        self.reply(deployer, self.deployment.raw())
        result = deployer.deploy(before, self.body, self.files.__getitem__)
        after = dict(before, deployments=[result["deployment"]])
        self.fixture.fixture.plan, self.fixture.fixture.after = plan, after
        proof = self.fixture.fixture.invoke(self.fixture.fixture.client())
        operation = apply.settle(plan, after, proof, "idle")
        reference = self.publish(operation, registered["declaration"])
        current = state.record(self.store, apply.published(plan, self.public, reference))
        for api, count in ((creator, 1), (uploader, 2), (deployer, 1)):
            self.assertEqual(api._client.open.call_count, count)
        return plan, after, current

    def test_default_and_two_keyed_complete_source_lifecycles(self):
        for lane in ("preview", "preview.design", "preview.review"):
            plan, after, current = self.deploy(lane)
            registered = plan["apply"]["registration"]
            self.fixture.registered, self.fixture.current = registered, current
            self.fixture.operation = current["last"]
            inspected = self.fixture.prepared()
            observed = {key: after[key] for key in ("parent", "previews", "preview")}
            self.fixture.origin = "https://" + after["preview"]["name"] + after["parent"]["suffix"]
            operation = self.fixture.invoke(self.fixture.client(inspected, observed))
            reference = self.publish(operation, registered["declaration"])
            reader.published(inspected, self.public, reference)
            self.assertEqual(state.read(self.store, registered)[0], current)
            active = copy.deepcopy(inspected["inspection"])
            active["request"].update(intent={"action": "dispose"}, expected=current["observed"])
            active["request"]["context"]["request"] = "b" * 64
            active["assessment"]["request"] = codec.digest("request", active["request"])
            reserved = state.reserve(self.store, active["request"], active["assessment"])
            disposal = discard.prepare(registered, reserved, observed, plan["policy"])
            deletion = writer.Writer("private-delete", disposal)
            deletion._client = mock.Mock()
            deletion._client.open.side_effect = TimeoutError("response lost")
            self.assertFalse(deletion.delete(observed)["acknowledged"])
            uncertain = state.record(self.store, discard.unknown(disposal, "unavailable"))
            resumed = discard.resume(disposal, uncertain)
            absent = dict(observed, preview=None, previews=[observed["previews"][1]])
            operation = discard.settle(resumed, absent, "idle")
            reference = self.publish(operation, registered["declaration"])
            tombstone = state.record(self.store, discard.published(resumed, self.public, reference))
            self.assertEqual((tombstone["revision"], tombstone["observed"]), (3, {"state": "absent"}))
            for original in (plan["apply"]["current"]["active"], reserved["active"]):
                if original == reserved["active"]:
                    self.assertEqual(state.reserve(self.store, original["request"], original["assessment"]), tombstone)
                else:
                    with self.assertRaises(Refusal):
                        state.reserve(self.store, original["request"], original["assessment"])
            absent_plan = self.fixture.prepared(tombstone)
            self.assertEqual(reader.Inspection(absent_plan, absent).read(None, None)["outcome"]["material"], {"state": "absent"})
            with self.assertRaises(Refusal):
                deletion.delete(observed)
            self.assertEqual(deletion._client.open.call_count, 1)
        self.assertTrue(all(key.startswith("lane/v1/") for key in self.store.objects))
        self.assertTrue(all(key.startswith("lane/v1/evidence/") for key in self.evidence.objects))

    def test_public_failure_does_not_advance_reserved_mutation_in_composition(self):
        held = self.content.plan
        registered = held["registration"]
        registration.write(self.store, registered, None)
        active = held["current"]["active"]
        current = state.reserve(self.store, active["request"], active["assessment"])
        operation = self.fixture.operation
        self.response.status = 404
        with self.assertRaises(Refusal):
            reference = self.publish(operation, registered["declaration"])
            state.record(self.store, apply.published(self.deployment.plan, self.public, reference))
        self.assertEqual(state.read(self.store, registered)[0], current)
        self.assertEqual(current["revision"], 1)

    def test_degraded_new_content_preserves_previous_material_but_cannot_claim_current_content(self):
        plan, after, previous = self.deploy("preview")
        registered = plan["apply"]["registration"]
        active = copy.deepcopy(plan["apply"]["current"]["active"])
        active["request"]["context"].update(request="c" * 64, revision=previous["revision"])
        active["request"]["expected"] = previous["observed"]
        active["assessment"]["conditions"]["observed"] = previous["observed"]
        active["assessment"]["request"] = codec.digest("request", active["request"])
        current = state.reserve(self.store, active["request"], active["assessment"])
        held = apply.prepare(registered, current, self.body, self.files.__getitem__)
        second = apply.deployment_prepare(held, after, plan["policy"])
        operation = apply.unknown(second, "unverified")
        reference = self.publish(operation, registered["declaration"])
        uncertain = state.record(self.store, operation)
        self.assertEqual((uncertain["revision"], uncertain["observed"]), (previous["revision"], previous["observed"]))
        with self.assertRaises(Refusal):
            apply.published(second, self.public, reference)
        with self.assertRaises(Refusal):
            apply.deployment_checked(apply.resume(second, uncertain))
        self.fixture.current, self.fixture.operation = uncertain, previous["last"]
        with self.assertRaises(Refusal):
            self.fixture.prepared()

    def test_create_response_loss_preserves_reservation_without_a_second_attempt(self):
        held = self.content.plan
        registered = held["registration"]
        registration.write(self.store, registered, None)
        active = held["current"]["active"]
        current = state.reserve(self.store, active["request"], active["assessment"])
        before = self.deployment.before
        absent = {"parent": before["parent"], "preview": None, "previews": [before["previews"][1]]}
        plan = create.prepare(held, absent, self.deployment.policy)
        creator = create.Creator("private", plan)
        creator._client = mock.Mock()
        creator._client.open.side_effect = TimeoutError("private")
        self.assertFalse(creator.create(absent, self.body, self.files.__getitem__)["acknowledged"])
        with self.assertRaises(Refusal):
            creator.create(absent, self.body, self.files.__getitem__)
        operation = dict(active, outcome={"outcome": "unknown", "reason": "unavailable"})
        unknown = state.record(self.store, operation)
        self.assertEqual(unknown["active"], current["active"])
        self.assertEqual(unknown["revision"], current["revision"])
        with self.assertRaises(Refusal):
            create.prepare(dict(held, current=unknown), absent, self.deployment.policy)
        self.assertEqual(creator._client.open.call_count, 1)
