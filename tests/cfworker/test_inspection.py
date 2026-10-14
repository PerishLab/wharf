import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import apply, reader
from lib.refusal import Refusal
from lib.store.lane import public, publication, registration, state
from tests.cfworker import test_readback
from tests.lib.identity import test_publication
from tests.lib.store.memory import Memory


class Inspection(unittest.TestCase):
    def setUp(self):
        self.fixture = test_readback.Readback()
        self.fixture.setUp()
        proof = self.fixture.invoke(self.fixture.client())
        self.registered = self.fixture.plan["apply"]["registration"]
        self.operation = apply.settle(self.fixture.plan, self.fixture.after, proof, "idle")
        self.current = state.finished(self.fixture.plan["apply"]["current"], self.operation, self.registered)
        self.plan = self.prepared()
        self.observed = {key: self.fixture.after[key] for key in ("parent", "previews", "preview")}
        self.origin = "https://preview" + self.observed["parent"]["suffix"]

    def prepared(self, current=None):
        current = self.current if current is None else current
        active = copy.deepcopy({key: self.operation[key] for key in ("request", "assessment")})
        active["request"]["context"].update(request="a" * 64, revision=current["revision"])
        active["request"].update(intent={"action": "inspect"}, expected=None)
        active["assessment"]["request"] = codec.digest("request", active["request"])
        active["assessment"]["conditions"].update(observed=current["observed"], writer="idle")
        return reader.inspection({"registration": self.registered, "current": current, "inspection": active, "policy": self.fixture.plan["policy"]})

    def client(self, plan=None, observed=None):
        api = reader.Inspection(self.plan if plan is None else plan, self.observed if observed is None else observed)
        api._client = mock.Mock()
        def received(request, timeout):
            route = request.full_url.removeprefix(self.origin)
            immutable = mock.Mock(full_url=self.fixture.origin + route)
            response = self.fixture.received(immutable, timeout)
            response.url = request.full_url
            return response
        api._client.open.side_effect = received
        return api

    def invoke(self, api):
        return api.read(self.fixture.fixture.document, self.fixture.fixture.bodies.__getitem__)

    def test_current_routing_reads_all_stored_vetted_bytes_without_checkout_or_credentials(self):
        api = self.client()
        operation = self.invoke(api)
        self.assertEqual(operation["outcome"]["material"], self.operation["outcome"]["material"])
        self.assertEqual(operation["request"], self.plan["inspection"]["request"])
        self.assertFalse(hasattr(api, "_token"))
        self.assertEqual(len(api._client.open.call_args_list), len(self.fixture.fixture.fixture.files) - 1)
        for call in api._client.open.call_args_list:
            self.assertTrue(call.args[0].full_url.startswith(self.origin + "/"))
            self.assertEqual(call.args[0].get_method(), "GET")
            self.assertIsNone(call.args[0].get_header("Authorization"))
        self.assertNotIn("deployment", operation["outcome"])

    def test_three_explicit_current_origins(self):
        for lane in ("preview", "design", "review"):
            plan = copy.deepcopy(self.plan)
            target = dict(plan["current"]["target"], lane=lane)
            plan["registration"]["declaration"]["target"] = target
            operation = copy.deepcopy(self.operation)
            operation["request"]["context"]["requested"] = target
            operation["assessment"]["request"] = codec.digest("request", operation["request"])
            plan["current"].update(target=target, registration=registration.digest(plan["registration"]), last=operation)
            plan["inspection"]["request"]["context"]["requested"] = target
            plan["inspection"]["assessment"]["request"] = codec.digest("request", plan["inspection"]["request"])
            observed = copy.deepcopy(self.observed)
            observed["preview"]["name"] = lane
            observed["previews"][0]["name"] = lane
            self.origin = "https://" + lane + observed["parent"]["suffix"]
            self.assertEqual(self.invoke(self.client(plan, observed))["request"]["context"]["requested"], target)

    def test_verified_absence_needs_no_handoff_checkout_or_http(self):
        current = state.initial(self.registered)
        plan = self.prepared(current)
        observed = dict(self.observed, preview=None, previews=[self.observed["previews"][1]])
        api = self.client(plan, observed)
        self.assertEqual(api.read(None, None)["outcome"], {"outcome": "applied", "material": {"state": "absent"}, "writer": "idle"})
        api._client.open.assert_not_called()
        with self.assertRaises(Refusal):
            api.read({}, None)

    def test_control_plane_presence_is_not_content_and_missing_selection_is_not_verified_material(self):
        with self.assertRaises(Refusal):
            self.client(self.prepared(state.initial(self.registered)))
        with self.assertRaises(Refusal):
            self.client(observed=dict(self.observed, preview=None, previews=[self.observed["previews"][1]]))
        api = self.client()
        api._client.open.side_effect = TimeoutError("private")
        with self.assertRaises(Refusal):
            self.invoke(api)
        self.assertEqual(api._client.open.call_count, 1)

    def test_busy_unknown_stale_disabled_or_changed_assessment_cannot_claim_current_content(self):
        for field, value in (("revision", 1), ("intent", {"action": "dispose"}), ("writer", "unknown"), ("observed", {"state": "absent"}), ("enabled", False)):
            plan = copy.deepcopy(self.plan)
            if field == "revision":
                plan["inspection"]["request"]["context"][field] = value
            elif field == "intent":
                plan["inspection"]["request"][field] = value
            elif field == "enabled":
                plan["registration"][field] = value
            else:
                plan["inspection"]["assessment"]["conditions"][field] = value
            with self.assertRaises(Refusal):
                reader.inspection(plan)
        plan = dict(self.plan, current=self.fixture.plan["apply"]["current"])
        with self.assertRaises(Refusal):
            reader.inspection(plan)
        current = state.finished(plan["current"], apply.unknown(self.fixture.plan, "unverified"), self.registered)
        with self.assertRaises(Refusal):
            reader.inspection(dict(plan, current=current))

    def test_tampered_handoff_policy_and_bytes_never_return_partial_inspection(self):
        fixture = self.fixture.fixture
        key = fixture.document["files"]["index.html"]["blob"]
        fixture.bodies[key] = b"changed"
        api = self.client()
        with self.assertRaises(Refusal):
            self.invoke(api)
        api._client.open.assert_not_called()
        self.setUp()
        api = self.client()
        received = api._client.open.side_effect
        def changed(request, timeout):
            response = received(request, timeout)
            response.headers.replace_header("Content-Security-Policy", "default-src *")
            return response
        api._client.open.side_effect = changed
        with self.assertRaises(Refusal):
            self.invoke(api)

    def test_unknown_fields_parent_drift_and_projection_aliases_refuse(self):
        for plan in (dict(self.plan, token="private"), dict(self.plan, policy={}), dict(self.plan, current={})):
            with self.assertRaises(Refusal):
                self.client(plan)
        with self.assertRaises(Refusal):
            self.client(observed=dict(self.observed, parent={}))
        plan = reader.inspection(self.plan)
        plan["current"]["revision"] = 3
        self.assertEqual(self.plan["current"]["revision"], 2)

    def test_full_artifact_identity_cannot_be_substituted_using_a_matching_content_digest(self):
        plan = copy.deepcopy(self.plan)
        plan["current"]["last"]["outcome"]["material"]["artifact"]["source"]["commit"] = "0" * 40
        plan["current"]["last"]["request"]["intent"]["artifact"]["source"]["commit"] = "0" * 40
        last = plan["current"]["last"]
        last["assessment"]["request"] = codec.digest("request", last["request"])
        state.checked(plan["current"], self.registered)
        api = self.client(plan)
        with self.assertRaises(Refusal):
            self.invoke(api)
        api._client.open.assert_not_called()

    def test_current_read_refuses_redirects_partial_bytes_wrong_origin_and_mid_read_handoff_movement(self):
        for field, value in (("status", 302), ("status", 206), ("url", self.fixture.origin)):
            api = self.client()
            received = api._client.open.side_effect
            def changed(request, timeout):
                response = received(request, timeout)
                setattr(response, field, value)
                return response
            api._client.open.side_effect = changed
            with self.assertRaises(Refusal):
                self.invoke(api)
        api = self.client()
        received = api._client.open.side_effect
        fixture = self.fixture.fixture
        def moving(request, timeout):
            response = received(request, timeout)
            if api._client.open.call_count == len(self.fixture.fixture.fixture.files) - 1:
                fixture.bodies[fixture.document["files"]["index.html"]["blob"]] = b"changed"
            return response
        api._client.open.side_effect = moving
        with self.assertRaises(Refusal):
            self.invoke(api)

    def test_inspection_transport_is_proxy_free_and_redirect_refusing(self):
        with mock.patch("urllib.request.build_opener") as build:
            reader.Inspection(self.plan, self.observed)
        self.assertEqual(build.call_args.args[0].proxies, {})
        with self.assertRaises(Refusal):
            build.call_args.args[1].redirect_request(None, None, 302, "redirect", {}, "https://foreign.invalid")

    def test_public_inspection_binds_exact_request_assessment_and_idle_writer(self):
        operation = self.invoke(self.client())
        record = {"schema": 2, "entry": {"kind": "operation", "value": operation}}
        response = test_publication.Response(codec.encode("record", record))
        connection = test_publication.Connection(response)
        public_reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            reference = publication.publish(Memory(), public_reader, self.registered["declaration"], record)
            self.assertEqual(reader.published(self.plan, public_reader, reference), operation)
            changed = copy.deepcopy(operation)
            changed["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
            response.body = codec.encode("record", dict(record, entry={"kind": "operation", "value": changed}))
            with self.assertRaises(Refusal):
                reader.published(self.plan, public_reader, public_reader.reference(codec.digest("record", dict(record, entry={"kind": "operation", "value": changed}))))
            response.status = 404
            with self.assertRaises(Refusal):
                reader.published(self.plan, public_reader, reference)

    def test_public_inspection_refuses_full_artifact_substitution_and_active_writer(self):
        operation = self.invoke(self.client())
        response = test_publication.Response(b"")
        connection = test_publication.Connection(response)
        public_reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        with mock.patch("lib.store.lane.public.http.client.HTTPSConnection", return_value=connection):
            for field in ("source", "writer"):
                changed = copy.deepcopy(operation)
                if field == "source":
                    changed["outcome"]["material"]["artifact"]["source"]["commit"] = "0" * 40
                else:
                    changed["outcome"]["writer"] = "active"
                record = {"schema": 2, "entry": {"kind": "operation", "value": changed}}
                response.body = codec.encode("record", record)
                reference = public_reader.reference(codec.digest("record", record))
                publication.settle(public_reader, self.registered["declaration"], reference, operation["request"])
                with self.assertRaises(Refusal):
                    reader.published(self.plan, public_reader, reference)
