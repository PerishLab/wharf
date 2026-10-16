import copy
import json
import unittest
from unittest import mock

from lib.content import resources
from lib.process.domains import routing
from lib.refusal import Refusal
from tests.lib.process import test_cfworker_observation
from tests.lib.process.test_cfworker import Response


def qualification(observed, deployed):
    catalogs = []
    for preview in observed["previews"]:
        values = observed["deployments"] if preview == observed["preview"] else []
        catalogs.append({"preview": dict(preview), "deployments": [{"id": item["id"], "prefix": item["id"][:8]} for item in values]})
    return copy.deepcopy({"status": observed, "deployment": deployed, "catalogs": catalogs})


class Routing(unittest.TestCase):
    def test_sibling_transport_refuses_foreign_context_before_io(self):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        api = fixture.client()
        parent = fixture.parent
        held = {"id": fixture.policy["id"], "name": parent["name"], "account": fixture.selected["account"],
                "suffix": parent["subdomain"]["preview_url_suffix"]}
        selected = {"id": "preview-id", "name": "preview", "parent": held["id"]}
        sibling = {"id": "review-id", "name": "review", "parent": held["id"]}
        status = {"parent": held, "preview": selected, "previews": [selected, sibling]}
        for context in (None, (), (sibling,), (sibling, {}), (dict(sibling, id="foreign-id"), status),
                        (sibling, dict(status, parent=dict(held, account="f" * 32))),
                        (sibling, dict(status, previews=[sibling])), (sibling, dict(status, preview=None))):
            self.assertRaises(Refusal, api.namespace_page, 1, context)
        for number in (None, True, 0, 11):
            self.assertRaises(Refusal, api.namespace_page, number, (sibling, status))
        for suffix in ("https://foreign.invalid", "/previews/review/deployments?page=1&per_page=100",
                       "/previews/review-id/deployments/" + fixture.deployment["id"]):
            self.assertRaises(Refusal, api.reply, suffix, sibling, status)
        api._client.open.assert_not_called()

    def test_namespace_bound_and_private_size_budget_refuse(self):
        api = mock.Mock()
        value = dict(self.observed, previews=self.observed["previews"] * 51)
        self.assertRaises(Refusal, routing.read, api, value)
        api.namespace_page.assert_not_called()
        value = qualification(self.observed, self.deployed)
        value["catalogs"][0]["private"] = "x" * 65536
        self.assertRaisesRegex(Refusal, "bounded", routing.checked, value, self.observed, self.deployed)

    def protocol(self, change):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        api = fixture.client()
        original = api._client.open.side_effect
        calls = {}
        def received(request, timeout):
            url = request.full_url
            calls[url] = calls.get(url, 0) + 1
            with original(request, timeout) as response:
                reply = json.load(response)
            change(reply, url, calls[url])
            return Response(json.dumps(reply).encode(), url)
        api._client.open.side_effect = received
        return fixture, api

    def test_point_get_malformed_url_or_foreign_identity_refuses(self):
        for change in ({"preview_id": "foreign"}, {"preview_name": None}, {"urls": []},
                       {"urls": ["https://foreign.invalid"]}, {"id": "latest"}):
            def changed(reply, url, count):
                if "/deployments/" in url:
                    reply["result"].update(change)
            fixture, api = self.protocol(changed)
            self.assertRaises(Refusal, api.observe, fixture.policy, fixture.deployment["id"])

    def test_complete_sibling_namespace_is_read_and_movement_or_incomplete_refuses(self):
        for mode in ("stable", "movement", "incomplete", "collision"):
            def changed(reply, url, count):
                if "/previews?page=" in url:
                    reply["result"].append({"id": "review-id", "name": "review", "slug": "review", "urls": []})
                    reply["result_info"].update(count=2, total_count=2)
                if "/previews/review-id/deployments?" in url:
                    reply["result"] = []
                    reply["result_info"].update(count=0, total_count=0)
                    if mode == "incomplete":
                        reply["result_info"]["total_count"] = 1
                    if mode == "collision" or mode == "movement" and count > 1:
                        identity = ("12345678" if mode == "collision" else "abcdefab") + "-9999-1234-1234-000000000002"
                        reply["result"] = [{"id": identity, "urls": ["https://" + identity[:8] + "-crest-development.perishlab.workers.dev"]}]
                        reply["result_info"].update(count=1, total_count=1)
            fixture, api = self.protocol(changed)
            if mode == "stable":
                value = api.observe(fixture.policy, fixture.deployment["id"])
                self.assertEqual(len(value["catalogs"]), 2)
                self.assertEqual(value["catalogs"][1]["deployments"], [])
                paths = [call.args[0].full_url for call in api._client.open.call_args_list]
                self.assertEqual(sum("/previews/review-id/deployments?" in path for path in paths), 2)
            else:
                self.assertRaises(Refusal, api.observe, fixture.policy, fixture.deployment["id"])

    def setUp(self):
        parent = {"id": "parent-id", "suffix": "-crest-development.perishlab.workers.dev"}
        preview = {"id": "selected-id", "name": "design", "parent": "parent-id"}
        sibling = {"id": "sibling-id", "name": "review", "parent": "parent-id"}
        self.deployed = {"id": "abcdefab-1234-1234-1234-000000000001", "preview": "selected-id", "parent": "parent-id"}
        self.observed = {"parent": parent, "preview": preview, "previews": [preview, sibling], "deployments": [self.deployed]}

    def test_complete_finite_namespace_is_detached_and_keeps_full_uuid(self):
        value = qualification(self.observed, self.deployed)
        self.assertEqual(routing.checked(value, self.observed, self.deployed),
                         "https://abcdefab-crest-development.perishlab.workers.dev")
        self.assertEqual(value["deployment"]["id"], self.deployed["id"])
        for change in ({}, dict(value, catalogs=[]), dict(value, deployment=dict(self.deployed, id="latest")),
                       dict(value, status=dict(self.observed, deployments=[])), dict(value, private="hidden")):
            self.assertRaises(Refusal, routing.checked, change, self.observed, self.deployed)

    def test_sibling_and_selected_prefix_collisions_refuse(self):
        for index in (0, 1):
            value = qualification(self.observed, self.deployed)
            value["catalogs"][index]["deployments"].append({"id": "abcdefab-9999-1234-1234-000000000002", "prefix": "abcdefab"})
            self.assertRaisesRegex(Refusal, "ambiguous", routing.checked, value, self.observed, self.deployed)
        value = qualification(self.observed, self.deployed)
        value["catalogs"][1]["deployments"] = [dict(value["catalogs"][0]["deployments"][0])]
        self.assertRaisesRegex(Refusal, "ambiguous", routing.checked, value, self.observed, self.deployed)

    def test_mutable_preview_name_conflicts_and_id_aliases_refuse(self):
        for name in ("abcdefab", "selected-id"):
            observed = copy.deepcopy(self.observed)
            observed["previews"][1]["name"] = name
            self.assertRaises(Refusal, routing.checked, qualification(observed, self.deployed), observed, self.deployed)

    def test_missing_foreign_or_malformed_catalogs_refuse(self):
        original = qualification(self.observed, self.deployed)
        for catalogs in (None, original["catalogs"][:1], original["catalogs"] * 2, list(reversed(original["catalogs"]))):
            self.assertRaises(Refusal, routing.checked, dict(original, catalogs=catalogs), self.observed, self.deployed)
        for prefix in (None, True, "ABCDEFAB", "abcdefa", "abcdefabc", "12345678", "latest", "../other"):
            value = copy.deepcopy(original)
            value["catalogs"][0]["deployments"][0]["prefix"] = prefix
            self.assertRaises(Refusal, routing.checked, value, self.observed, self.deployed)
        for values in (None, [], [dict(original["catalogs"][0]["deployments"][0], private="hidden")],
                       [{"id": "latest", "prefix": "abcdefab"}], original["catalogs"][0]["deployments"] * 101):
            value = copy.deepcopy(original)
            value["catalogs"][0]["deployments"] = values
            self.assertRaises(Refusal, routing.checked, value, self.observed, self.deployed)

    def test_exact_get_omissions_use_immutable_endpoint_without_relaxing_post(self):
        fixture = test_cfworker_observation.Observation()
        fixture.setUp()
        fixture.deployment.pop("preview_id")
        fixture.deployment.pop("preview_name")
        api = fixture.client()
        value = api.observe(fixture.policy, fixture.deployment["id"])
        calls = api._client.open.call_args_list
        exact = [call.args[0] for call in calls if "/deployments/" in call.args[0].full_url]
        self.assertEqual(len(exact), 1)
        self.assertIn("/previews/preview-id/deployments/" + fixture.deployment["id"], exact[0].full_url)
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in calls))
        self.assertEqual(value["deployment"]["id"], fixture.deployment["id"])
        self.assertEqual(value["catalogs"][0]["deployments"][0]["prefix"], fixture.deployment["id"][:8])

    def test_actual_endpoint_url_corroborates_full_uuid_without_embedded_fields(self):
        fixture = resources.read_json("identity/deployment-observation.json")
        raw = fixture["catalog"]["result"][0]
        parent = {"suffix": "-crest-development.862457174.workers.dev"}
        self.assertEqual(routing.project(raw, fixture["context"], parent),
                         {"id": raw["id"], "prefix": "7f201597"})
        for urls in (None, [], [raw["urls"][0] + "/"], ["https://foreign.invalid"],
                     ["https://" + raw["id"] + parent["suffix"]], raw["urls"] * 2):
            with self.subTest(urls=urls), self.assertRaises(Refusal):
                routing.project(dict(raw, urls=urls), fixture["context"], parent)
        for key in ("preview_id", "preview_name"):
            for value in (None, "foreign"):
                self.assertRaises(Refusal, routing.project, dict(raw, **{key: value}), fixture["context"], parent)
