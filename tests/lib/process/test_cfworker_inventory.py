import copy
import json
import unittest
import urllib.error
from unittest import mock

from lib.process.cfworker import inventory, observation, reader
from lib.refusal import Refusal
from tests.lib.process.test_cfworker import Response
from tests.lib.process import test_cfworker_observation
from tests.lib.store.test_lane import operation, registered


def listing(values, total=None, number=1, size=100):
    return {"success": True, "errors": None, "result": values, "result_info": {"page": number, "count": len(values), "total_count": len(values) if total is None else total, "per_page": size}}


class Inventory(unittest.TestCase):
    def setUp(self):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        self.policy = fixture.policy
        self.parent = fixture.parent
        self.held = observation.parent(self.parent, fixture.selected, self.policy)

    def preview(self, name, identity=None):
        return {"id": identity or name + "-id", "name": name, "slug": name, "urls": ["https://" + name + self.held["suffix"]]}

    def client(self, replies, lane="preview"):
        api = reader.Reader("private-test-token", registered(operation(lane)))
        api._client = mock.Mock()
        values = iter(replies)
        api._client.open.side_effect = lambda request, timeout: Response(json.dumps(next(values)).encode(), request.full_url)
        return api

    def parent_reply(self):
        return {"success": True, "errors": None, "result": self.parent}

    def test_empty_complete_inventory_establishes_selected_observed_absence(self):
        api = self.client([self.parent_reply(), listing([]), listing([]), self.parent_reply()])
        self.assertEqual(api.status(self.policy), {"parent": self.held, "previews": [], "preview": None})
        calls = api._client.open.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[1].args[0].full_url.endswith("/previews?page=1&per_page=100"))
        self.assertTrue(calls[2].args[0].full_url.endswith("/previews?page=1&per_page=100"))
        for call in calls:
            self.assertEqual(call.args[0].get_method(), "GET")
            self.assertIsNone(call.args[0].data)
            self.assertEqual(call.kwargs, {"timeout": 30})

    def test_three_explicit_selections_have_distinct_projected_identities(self):
        values = [self.preview(name) for name in ("preview", "design", "review")]
        for lane in ("preview", "design", "review"):
            selected = next(item for item in values if item["name"] == lane)
            api = self.client([self.parent_reply(), listing(values), {"success": True, "errors": [], "result": selected}, listing(list(reversed(values))), self.parent_reply()], lane)
            held = api.status(self.policy)
            self.assertEqual(held["preview"]["id"], selected["id"])
            self.assertEqual(len(held["previews"]), 3)
            self.assertEqual(api._client.open.call_count, 5)
            self.assertNotIn("deployment", held)
            self.assertNotIn("latest", json.dumps(held))

    def test_absent_selected_lane_retains_two_siblings(self):
        values = [self.preview(name) for name in ("design", "review")]
        api = self.client([self.parent_reply(), listing(values), listing(values), self.parent_reply()])
        held = api.status(self.policy)
        self.assertIsNone(held["preview"])
        self.assertEqual([item["name"] for item in held["previews"]], ["design", "review"])

    def test_complete_two_page_status_scans_twice(self):
        values = [self.preview("lane-" + str(number)) for number in range(101)]
        first, second = listing(values[:100], 101), listing(values[100:], 101, 2)
        api = self.client([self.parent_reply(), first, second, first, second, self.parent_reply()])
        held = api.status(self.policy)
        self.assertEqual(len(held["previews"]), 101)
        self.assertIsNone(held["preview"])
        calls = [call.args[0].full_url for call in api._client.open.call_args_list]
        self.assertTrue(calls[2].endswith("/previews?page=2&per_page=100"))
        self.assertEqual(calls[1:3], calls[3:5])

    def test_bounded_provider_page_width_is_supported(self):
        values = [self.preview("lane-" + str(number)) for number in range(21)]
        api = self.client([listing(values[:20], 21, 1, 20), listing(values[20:], 21, 2, 20)])
        self.assertEqual(len(inventory.read(api, self.held)), 21)

    def test_invalid_page_selectors_never_reach_network(self):
        api = self.client([])
        for number in (None, True, False, 0, -1, 1.0, "1", inventory.PAGES + 1):
            with self.subTest(number=number), self.assertRaises(Refusal):
                api.page(number)
        for suffix in ("/previews", "/previews?page=0&per_page=100", "/previews?page=01&per_page=100", "/previews?page=1&per_page=101", "/previews?page=1&per_page=100&name=another"):
            with self.subTest(suffix=suffix), self.assertRaises(Refusal):
                api.reply(suffix)
        api._client.open.assert_not_called()

    def test_invalid_policy_before_network(self):
        api = self.client([])
        with self.assertRaises(Refusal):
            api.status({"id": "../production", "subdomain": "perishlab"})
        api._client.open.assert_not_called()

    def test_missing_or_wrong_page_metadata_refuses(self):
        base = listing([])
        for key in base["result_info"]:
            reply = copy.deepcopy(base)
            del reply["result_info"][key]
            with self.subTest(key=key), self.assertRaises(Refusal):
                inventory.read(self.client([reply]), self.held)
        for key, value in (("page", True), ("page", 2), ("count", 1), ("count", -1), ("total_count", 1.0), ("total_count", 1), ("per_page", 0), ("per_page", 101), ("total_pages", 3), ("total_pages", True)):
            reply = copy.deepcopy(base)
            reply["result_info"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                inventory.read(self.client([reply]), self.held)

    def test_empty_total_pages_zero_or_one_is_supported(self):
        for number in (0, 1):
            reply = listing([])
            reply["result_info"]["total_pages"] = number
            self.assertEqual(inventory.read(self.client([reply]), self.held), [])

    def test_incomplete_first_page_never_establishes_absence(self):
        for values, total in (([], 1), ([self.preview("other")], 101)):
            api = self.client([listing(values, total)])
            with self.assertRaises(Refusal):
                inventory.read(api, self.held)
            self.assertEqual(api._client.open.call_count, 1)

    def test_finite_page_and_item_budgets_refuse_before_continuation(self):
        for total, size in ((1001, 100), (201, 20)):
            values = [self.preview("lane-" + str(number)) for number in range(size)]
            api = self.client([listing(values, total, 1, size)])
            with self.assertRaises(Refusal):
                inventory.read(api, self.held)
            self.assertEqual(api._client.open.call_count, 1)

    def test_cross_page_duplicates_refuse(self):
        values = [self.preview("lane-" + str(number)) for number in range(100)]
        for last in (values[0], self.preview("different", values[0]["id"])):
            api = self.client([listing(values, 101), listing([last], 101, 2)])
            with self.assertRaises(Refusal):
                inventory.read(api, self.held)

    def test_changed_total_or_width_between_pages_refuses(self):
        values = [self.preview("lane-" + str(number)) for number in range(100)]
        for last in (listing([self.preview("a"), self.preview("b")], 102, 2), listing([self.preview("a")], 51, 2, 50)):
            api = self.client([listing(values, 101), last])
            with self.assertRaises(Refusal):
                inventory.read(api, self.held)

    def test_foreign_or_malformed_preview_identity_refuses(self):
        values = (None, [], {}, dict(self.preview("other"), id="../private"), dict(self.preview("other"), slug="another"), dict(self.preview("other"), urls=["https://attacker.invalid"]), self.preview("a" * 60), self.preview("../parent"))
        for value in values:
            with self.subTest(value=value), self.assertRaises(Refusal):
                inventory.read(self.client([listing([value])]), self.held)

    def test_malformed_list_or_unsuccessful_envelope_refuses(self):
        values = ({"success": True, "errors": None, "result": {}}, {"success": False, "errors": [], "result": []}, {"success": True, "errors": ["denied"], "result": []}, {"success": True, "result": []}, dict(listing([]), result_info=[]))
        for value in values:
            with self.assertRaises(Refusal):
                inventory.read(self.client([value]), self.held)
            with self.assertRaises(Refusal):
                inventory.page(value, 1)
        for number in (None, True, 0, 1.0, "1", inventory.PAGES + 1):
            with self.assertRaises(Refusal):
                inventory.page(listing([]), number)

    def test_private_fields_are_omitted_and_projection_is_detached(self):
        value = dict(self.preview("preview"), env={"PRIVATE": "never-publish"}, annotations={"commit": "not-content-proof"})
        replies = [self.parent_reply(), listing([value]), {"success": True, "errors": [], "result": value}, listing([value]), self.parent_reply()]
        held = self.client(replies).status(self.policy)
        self.assertNotIn("never-publish", json.dumps(held))
        self.assertNotIn("not-content-proof", json.dumps(held))
        value["id"] = "moved"
        self.parent["id"] = "moved"
        self.assertEqual(held["preview"]["id"], "preview-id")
        self.assertEqual(held["parent"]["id"], self.policy["id"])

    def test_selected_direct_read_or_repeated_catalog_movement_refuses(self):
        first = self.preview("preview")
        changed = dict(first, id="changed")
        direct = {"success": True, "errors": [], "result": first}
        for replies in (
            [self.parent_reply(), listing([first]), dict(direct, result=changed)],
            [self.parent_reply(), listing([first]), direct, listing([changed])],
            [self.parent_reply(), listing([]), listing([first])],
        ):
            with self.assertRaises(Refusal):
                self.client(replies).status(self.policy)

    def test_parent_policy_reread_movement_refuses(self):
        changed = dict(self.parent_reply(), result=dict(self.parent, id="changed"))
        api = self.client([self.parent_reply(), listing([]), listing([]), changed])
        with self.assertRaises(Refusal):
            api.status(self.policy)

    def test_failure_at_every_read_is_unknown_not_absence_and_never_retries(self):
        for step in range(4):
            api = self.client([self.parent_reply(), listing([]), listing([]), self.parent_reply()])
            original = api._client.open.side_effect
            calls = []

            def read(request, timeout):
                calls.append(request)
                if len(calls) == step + 1:
                    raise urllib.error.HTTPError("private-test-token", 404, "private-test-token", {}, None)
                return original(request, timeout)

            api._client.open.side_effect = read
            with self.assertRaises(Refusal) as raised:
                api.status(self.policy)
            self.assertNotIn("private-test-token", str(raised.exception))
            self.assertEqual(len(calls), step + 1)


if __name__ == "__main__":
    unittest.main()
