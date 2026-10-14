import copy
import json
import unittest
from unittest import mock

from lib.process.cfworker import inventory, observation, reader
from lib.refusal import Refusal
from tests.lib.process import test_cfworker_inventory, test_cfworker_observation
from tests.lib.process.test_cfworker import Response
from tests.lib.store.test_lane import operation, registered


class History(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        self.policy, self.parent = fixture.policy, fixture.parent
        self.held = observation.parent(self.parent, fixture.selected, self.policy)
        self.preview = dict(fixture.preview, urls=[])
        self.selected = observation.resource(self.preview, fixture.selected, self.held)

    def raw(self, number=0):
        return {"id": f"12345678-1234-1234-1234-{number:012x}", "preview_id": self.preview["id"], "preview_name": self.preview["name"]}

    def object(self, value):
        return {"success": True, "errors": [], "result": value}

    def status(self):
        return [self.object(self.parent), test_cfworker_inventory.listing([self.preview]), self.object(self.preview), test_cfworker_inventory.listing([self.preview]), self.object(self.parent)]

    def client(self, replies, lane="preview"):
        api = reader.Reader("private-history-credential", registered(operation(lane)))
        api._client = mock.Mock()
        values = iter(replies)
        api._client.open.side_effect = lambda request, timeout: Response(json.dumps(next(values)).encode(), request.full_url)
        return api

    def full(self, pages):
        return self.client(self.status() + pages + pages + self.status())

    def test_undeployed_three_explicit_previews_have_complete_empty_history(self):
        for lane in ("preview", "design", "review"):
            self.preview.update(name=lane, slug=lane)
            api = self.client(self.status() + [test_cfworker_inventory.listing([])] * 2 + self.status(), lane)
            held = api.history(self.policy)
            self.assertEqual(held["deployments"], [])
            self.assertEqual(held["preview"]["name"], lane)
            calls = api._client.open.call_args_list
            self.assertEqual(len(calls), 12)
            self.assertTrue(calls[5].args[0].full_url.endswith("/previews/" + lane + "/deployments?page=1&per_page=100"))
            for call in calls:
                self.assertEqual(call.args[0].get_method(), "GET")
                self.assertIsNone(call.args[0].data)
                self.assertEqual(call.kwargs, {"timeout": 30})

    def test_catalog_paginates_completely_twice_and_omits_private_fields(self):
        values = [dict(self.raw(number), env={"SECRET": "private-value"}, urls=["https://foreign.invalid"], annotations={"commit": "not-proof"}) for number in range(21)]
        pages = [test_cfworker_inventory.listing(values[:20], 21, 1, 20), test_cfworker_inventory.listing(values[20:], 21, 2, 20)]
        held = self.full(pages).history(self.policy)
        self.assertEqual(len(held["deployments"]), 21)
        self.assertEqual(set(held["deployments"][0]), {"id", "preview", "parent"})
        for private in ("private-history-credential", "private-value", "foreign.invalid", "not-proof"):
            self.assertNotIn(private, json.dumps(held))
        values[0]["id"] = "changed"
        self.assertEqual(held["deployments"][0]["id"], self.raw()["id"])

    def test_catalog_order_is_not_movement(self):
        first = test_cfworker_inventory.listing([self.raw(1), self.raw(0)])
        second = test_cfworker_inventory.listing([self.raw(0), self.raw(1)])
        api = self.client(self.status() + [first, second] + self.status())
        self.assertEqual([item["id"] for item in api.history(self.policy)["deployments"]], [self.raw(0)["id"], self.raw(1)["id"]])

    def test_absent_selected_preview_never_becomes_empty_deployment_history(self):
        api = self.client([self.object(self.parent), test_cfworker_inventory.listing([]), test_cfworker_inventory.listing([]), self.object(self.parent)])
        with self.assertRaises(Refusal):
            api.history(self.policy)
        self.assertEqual(api._client.open.call_count, 4)

    def test_optional_undeployed_urls_do_not_relax_strict_deployed_address_observation(self):
        for urls in (None, []):
            value = dict(self.preview, urls=urls)
            self.assertEqual(observation.resource(value, {"name": "preview"}, self.held), self.selected)
            with self.assertRaises(Refusal):
                observation.preview(value, {"name": "preview"}, self.held)
        for urls in (["https://foreign.invalid"], ["https://preview" + self.held["suffix"], "https://foreign.invalid"], "not-list"):
            with self.assertRaises(Refusal):
                observation.resource(dict(self.preview, urls=urls), {"name": "preview"}, self.held)

    def test_invalid_policy_and_page_selectors_never_attempt_network(self):
        api = self.client([])
        with self.assertRaises(Refusal):
            api.history(dict(self.policy, id="../parent"))
        for number in (None, True, 0, -1, 1.0, "1", 11):
            with self.assertRaises(Refusal):
                api.deployment_page(number)
        for suffix in ("/previews/foreign/deployments?page=1&per_page=100", "/previews/preview/deployments", "/previews/preview/deployments?page=01&per_page=100", "/previews/preview/deployments?page=1&per_page=101", "/previews/preview/deployments/latest"):
            with self.assertRaises(Refusal):
                api.reply(suffix)
        api._client.open.assert_not_called()

    def test_incomplete_missing_metadata_or_over_native_bound_catalog_refuses(self):
        base = test_cfworker_inventory.listing([])
        variants = [dict(base, result_info={}), test_cfworker_inventory.listing([], 1), test_cfworker_inventory.listing([self.raw(number) for number in range(100)], 101)]
        for reply in variants:
            api = self.client([reply])
            with self.assertRaises(Refusal):
                inventory.deployments(api, self.selected)
            self.assertEqual(api._client.open.call_count, 1)

    def test_cross_page_duplicates_and_pagination_movement_refuse(self):
        first = test_cfworker_inventory.listing([self.raw()], 2, 1, 1)
        second = test_cfworker_inventory.listing([self.raw()], 2, 2, 1)
        changed = test_cfworker_inventory.listing([self.raw(1)], 3, 2, 1)
        for reply in (second, changed):
            with self.assertRaises(Refusal):
                inventory.deployments(self.client([first, reply]), self.selected)

    def test_foreign_malformed_repeated_or_latest_deployment_identity_refuses(self):
        values = [None, {}, dict(self.raw(), id="latest"), dict(self.raw(), preview_id="foreign"), dict(self.raw(), preview_name="foreign"), dict(self.raw(), id=True)]
        for value in values:
            with self.assertRaises(Refusal):
                inventory.deployments(self.client([test_cfworker_inventory.listing([value])]), self.selected)
        with self.assertRaises(Refusal):
            inventory.deployments(self.client([test_cfworker_inventory.listing([self.raw(), self.raw()])]), self.selected)

    def test_repeated_history_or_parent_preview_sibling_movement_refuses(self):
        first = test_cfworker_inventory.listing([self.raw()])
        with self.assertRaises(Refusal):
            self.client(self.status() + [first, test_cfworker_inventory.listing([self.raw(1)])]).history(self.policy)
        for field in ("parent", "preview", "sibling"):
            replies = copy.deepcopy(self.status())
            if field == "parent":
                replies[0]["result"]["id"] = "foreign"
            elif field == "preview":
                for reply in replies[1:4]:
                    value = reply["result"]
                    (value[0] if isinstance(value, list) else value)["id"] = "recreated"
            else:
                sibling = dict(self.preview, id="sibling-id", name="sibling", slug="sibling")
                replies[1] = test_cfworker_inventory.listing([self.preview, sibling])
                replies[3] = test_cfworker_inventory.listing([self.preview, sibling])
            with self.assertRaises(Refusal):
                self.client(self.status() + [first, first] + replies).history(self.policy)

    def test_failure_at_each_read_never_retries_or_proves_empty_history(self):
        for step in range(12):
            api = self.full([test_cfworker_inventory.listing([])])
            original = api._client.open.side_effect
            calls = []
            def received(request, timeout):
                calls.append(request)
                if len(calls) == step + 1:
                    raise OSError("private-history-credential")
                return original(request, timeout)
            api._client.open.side_effect = received
            with self.assertRaises(Refusal):
                api.history(self.policy)
            self.assertEqual(api._client.open.call_count, step + 1)

    def test_finite_supplied_history_refuses_foreign_fields_and_detaches(self):
        projected = observation.deployment(self.raw(), self.selected, self.raw()["id"])
        held = inventory.history([projected], self.selected)
        projected["id"] = "changed"
        self.assertEqual(held[0]["id"], self.raw()["id"])
        for value in (None, {}, [dict(held[0], parent="foreign")], [dict(held[0], token="never")], held * 2):
            with self.assertRaises(Refusal):
                inventory.history(value, self.selected)
