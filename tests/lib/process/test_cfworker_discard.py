import copy
import json
import unittest
import urllib.error
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import discard, mapping, observation, writer
from lib.refusal import Refusal
from lib.store.lane import registration, state
from tests.lib.process import test_cfworker_observation
from tests.lib.process.test_cfworker import Response
from tests.lib.preview.test_store import Store
from tests.lib.store.test_lane import dispose, operation, registered


class Discard(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        self.policy = fixture.policy
        self.parent = observation.parent(fixture.parent, fixture.selected, self.policy)
        self.plan = self.prepared()

    def prepared(self, lane="preview"):
        value = operation(lane)
        held = registered(value)
        active = {key: value[key] for key in ("request", "assessment")}
        previous = state.finished(state.started(state.initial(held), active, held), value, held)
        deletion = dispose(previous)
        deletion["request"]["context"]["requested"]["lane"] = lane
        deletion["assessment"]["request"] = codec.digest("request", deletion["request"])
        reserved = state.started(previous, {key: deletion[key] for key in ("request", "assessment")}, held)
        values = [{"id": name + "-id", "name": name, "parent": self.parent["id"]} for name in ("preview", "design", "review")]
        selected = next(item for item in values if item["name"] == lane)
        before = {"parent": self.parent, "previews": values, "preview": selected}
        return discard.prepare(held, reserved, before, self.policy)

    def absent(self, plan=None):
        before = copy.deepcopy((plan or self.plan)["before"])
        before["previews"] = [item for item in before["previews"] if item != before["preview"]]
        before["preview"] = None
        return before

    def client(self, plan=None, body=None):
        api = writer.Writer("private-writer-token", plan or self.plan)
        api._client = mock.Mock()
        body = b'{"success":true,"errors":null,"result":null}' if body is None else body
        api._client.open.side_effect = lambda request, timeout: Response(body, request.full_url)
        return api

    def test_three_explicit_deletes_bind_only_immutable_selection(self):
        for lane in ("preview", "design", "review"):
            plan = self.prepared(lane)
            api = self.client(plan)
            result = api.delete(plan["before"])
            self.assertEqual(result, {"request": codec.digest("request", plan["current"]["active"]["request"]), "acknowledged": True})
            call = api._client.open.call_args
            request = call.args[0]
            self.assertEqual(request.get_method(), "DELETE")
            self.assertIsNone(request.data)
            base = "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/workers/crest-development/previews/"
            self.assertEqual(request.full_url, base + plan["before"]["preview"]["id"])
            self.assertEqual(call.kwargs, {"timeout": 30})
            self.assertEqual(request.get_header("Authorization"), "Bearer private-writer-token")
            settled = discard.settle(plan, self.absent(plan), "idle")
            self.assertEqual(settled["request"], plan["current"]["active"]["request"])
            self.assertEqual(settled["outcome"], {"outcome": "applied", "material": {"state": "absent"}, "writer": "idle"})

    def test_plan_is_detached_and_canonical_catalog_order_is_not_movement(self):
        original = copy.deepcopy(self.plan)
        plan = discard.checked(original)
        original["before"]["preview"]["id"] = "changed"
        original["registration"]["mapping"]["resource"] = "another"
        self.assertEqual(plan, self.plan)
        observed = copy.deepcopy(plan["before"])
        observed["previews"].reverse()
        self.assertTrue(self.client(plan).delete(observed)["acknowledged"])

    def test_non_dispose_stale_foreign_or_disabled_reservations_refuse(self):
        changes = [("phase", "vacant"), ("revision", 3), ("registration", "0" * 64), ("active", None)]
        for key, value in changes:
            plan = copy.deepcopy(self.plan)
            plan["current"][key] = value
            with self.subTest(key=key), self.assertRaises(Refusal):
                discard.checked(plan)
        for key, value in (("enabled", False), ("generation", "0" * 64)):
            plan = copy.deepcopy(self.plan)
            plan["registration"][key] = value
            with self.assertRaises(Refusal):
                discard.checked(plan)
        plan = copy.deepcopy(self.plan)
        active = plan["current"]["active"]
        active["request"]["intent"] = operation()["request"]["intent"]
        active["assessment"]["request"] = codec.digest("request", active["request"])
        with self.assertRaises(Refusal):
            discard.checked(plan)

    def test_unknown_recovery_may_settle_but_cannot_create_another_writer(self):
        plan = copy.deepcopy(self.plan)
        unknown = dict(plan["current"]["active"], outcome={"outcome": "unknown", "reason": "unavailable"})
        plan["current"] = state.finished(plan["current"], unknown, plan["registration"])
        plan = discard.checked(plan)
        self.assertEqual(plan["current"]["phase"], "unknown")
        with self.assertRaises(Refusal):
            self.client(plan)
        for condition in ("active", "unknown", None, True):
            with self.assertRaises(Refusal):
                discard.settle(plan, self.absent(plan), condition)
        self.assertEqual(discard.settle(plan, self.absent(plan), "idle")["request"], unknown["request"])

    def test_already_absent_selection_requires_no_delete(self):
        plan = copy.deepcopy(self.plan)
        plan["before"] = self.absent(plan)
        plan = discard.checked(plan)
        with self.assertRaises(Refusal):
            self.client(plan)
        self.assertEqual(discard.settle(plan, plan["before"], "idle")["outcome"]["material"], {"state": "absent"})

    def test_control_plane_presence_cannot_match_initial_absent_material(self):
        plan = copy.deepcopy(self.plan)
        plan["current"]["observed"] = {"state": "absent"}
        with self.assertRaises(Refusal):
            discard.checked(plan)
        plan["current"]["active"]["request"]["expected"] = {"state": "absent"}
        with self.assertRaises(Refusal):
            discard.checked(plan)

    def test_malformed_private_or_oversized_plans_never_authorize_network(self):
        for plan in (None, {}, dict(self.plan, token="never"), dict(self.plan, policy={"id": "../parent", "subdomain": "perishlab"}), dict(self.plan, before="x" * 65537)):
            with self.subTest(plan=type(plan).__name__), self.assertRaises(Refusal):
                writer.Writer("private-writer-token", plan)
        for token in (None, "", "with space", "line\nbreak", "\x7f", "非ascii"):
            with self.assertRaises(Refusal):
                writer.Writer(token, self.plan)

    def test_ambiguous_id_name_routing_refuses_before_network(self):
        for name in ("preview", "review"):
            plan = copy.deepcopy(self.plan)
            plan["before"]["preview"]["id"] = name
            for item in plan["before"]["previews"]:
                if item["name"] == "preview":
                    item["id"] = name
            with self.assertRaises(Refusal):
                self.client(plan)

    def test_pre_write_parent_selection_or_sibling_drift_never_deletes(self):
        for path in ("parent", "preview", "sibling"):
            api = self.client()
            observed = copy.deepcopy(self.plan["before"])
            if path == "parent":
                observed["parent"]["id"] = "another"
            elif path == "preview":
                observed["preview"]["id"] = "recreated"
            else:
                observed["previews"][1]["id"] = "changed"
            with self.subTest(path=path), self.assertRaises(Refusal):
                api.delete(observed)
            api._client.open.assert_not_called()
            self.assertTrue(api.delete(self.plan["before"])["acknowledged"])

    def test_attempted_writer_is_spent_after_success_or_response_loss(self):
        for error in (None, urllib.error.HTTPError("private-writer-token", 404, "private-writer-token", {}, None), urllib.error.URLError("private-writer-token"), TimeoutError("private-writer-token"), OSError("private-writer-token")):
            api = self.client()
            if error is not None:
                api._client.open.side_effect = error
            result = api.delete(self.plan["before"])
            self.assertEqual(result["acknowledged"], error is None)
            self.assertNotIn("private-writer-token", json.dumps(result))
            with self.assertRaises(Refusal):
                api.delete(self.plan["before"])
            self.assertEqual(api._client.open.call_count, 1)

    def test_wrong_endpoint_status_encoding_or_cleanup_remains_unacknowledged(self):
        for field, value in (("url", "https://attacker.invalid"), ("status", 204), ("status", 404), ("headers", {"Content-Encoding": "gzip"})):
            api = self.client()
            response = Response(b'{}', "unused")
            setattr(response, field, value)
            api._client.open.side_effect = None
            api._client.open.return_value = response
            self.assertFalse(api.delete(self.plan["before"])["acknowledged"])
        api = self.client()
        api._client.open.side_effect = None
        response = mock.MagicMock()
        response.__exit__.side_effect = OSError("private-writer-token")
        api._client.open.return_value = response
        self.assertFalse(api.delete(self.plan["before"])["acknowledged"])
        self.assertEqual(api._client.open.call_count, 1)

    def test_malformed_success_envelopes_are_not_acknowledgements(self):
        bodies = [b'{}', b'[]', b'null', b'bad', b'\xff', b'{"success":true,"success":false}', b'x' * 1048577]
        bodies.extend(json.dumps(value).encode() for value in ({"success": 1, "errors": [], "result": None}, {"success": True, "result": None}, {"success": True, "errors": [], "result": {"id": "another"}}, {"success": True, "errors": False, "result": None}, {"success": True, "errors": [], "result": []}))
        for body in bodies:
            api = self.client(body=body)
            with self.subTest(body=body[:40]):
                self.assertFalse(api.delete(self.plan["before"])["acknowledged"])
                self.assertEqual(api._client.open.call_count, 1)

    def test_acknowledgement_alone_never_settles_and_drift_is_not_silently_repaired(self):
        self.assertTrue(self.client().delete(self.plan["before"])["acknowledged"])
        values = [self.plan["before"], dict(self.absent(), previews=[]), dict(self.absent(), parent={}), dict(self.absent(), preview={})]
        changed = self.absent()
        changed["previews"][0]["id"] = "recreated"
        values.append(changed)
        for observed in values:
            with self.assertRaises(Refusal):
                discard.settle(self.plan, observed, "idle")

    def test_invalid_catalog_and_selection_shapes_refuse(self):
        for values in (None, {}, [None], self.plan["before"]["previews"] * 2, [{"id": "../other", "name": "other", "parent": self.parent["id"]}], [{"id": "other-id", "name": "other", "parent": "foreign"}], [{"id": "other-id", "name": "a" * 63, "parent": self.parent["id"]}], [{"id": "other-id", "name": "other", "parent": self.parent["id"], "env": "private"}]):
            with self.assertRaises(Refusal):
                discard.status(dict(self.absent(), previews=values), mapping.selected(self.plan["registration"]), self.policy)

    def test_response_loss_settlement_retains_exact_request_and_tombstone(self):
        api = self.client()
        api._client.open.side_effect = TimeoutError("private-writer-token")
        self.assertFalse(api.delete(self.plan["before"])["acknowledged"])
        bucket = Store()
        held = self.plan["registration"]
        registration.write(bucket, held, None)
        bucket.put("lane/v1/PerishLab/crest/crest/preview/current.json", json.dumps(self.plan["current"]).encode())
        settled = discard.settle(self.plan, self.absent(), "idle")
        tombstone = state.record(bucket, settled)
        self.assertEqual((tombstone["revision"], tombstone["observed"], tombstone["active"]), (3, {"state": "absent"}, None))
        self.assertEqual(tombstone["last"]["request"], self.plan["current"]["active"]["request"])
        self.assertEqual(state.started(tombstone, self.plan["current"]["active"], held), tombstone)
        value = operation()
        with self.assertRaises(Refusal):
            state.started(tombstone, {key: value[key] for key in ("request", "assessment")}, held)

    def test_no_proxy_or_redirect_and_no_credential_projection(self):
        with mock.patch.object(writer.urllib.request, "build_opener") as factory:
            api = writer.Writer("private-writer-token", self.plan)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        with self.assertRaises(Refusal):
            factory.call_args.args[1].redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid")
        self.assertNotIn("private-writer-token", repr(api))
        self.assertNotIn("private-writer-token", json.dumps(self.plan))


if __name__ == "__main__":
    unittest.main()
