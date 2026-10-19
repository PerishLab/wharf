import copy
import json
import unittest
import urllib.error
from unittest import mock

from lib.content import resources
from lib.process.cfworker import inventory, mapping, observation
from lib.process.domains import context, routing, versions
from lib.refusal import Refusal
from tests.domains import fixtures
from tests.domains.test_base import pins as base_pins
from tests.domains.test_routing import qualification
from tests.lib.process.test_cfworker import DEPLOYMENT, Response
from tests.lib.store.test_lane import operation, registered


class Versions(unittest.TestCase):
    def setUp(self):
        self.fixture, self.api = fixtures.provider()
        self.reader = versions.Reader("private-test-token", self.fixture.registered, self.fixture.policy)
        self.reader._client = self.api._client
        self.parent = context.projected(self.fixture.policy, self.fixture.selected)

    def reply(self, values, page=1, total=None):
        return {"success": True, "errors": [], "result": values,
                "result_info": {"page": page, "count": len(values), "total_count": len(values) if total is None else total, "per_page": 50}}

    def item(self, requested):
        return {"id": requested, "urls": [observation.address(self.parent, requested, requested[:8]),
                "https://" + requested[:8] + ".lane.crest.perish.uk"]}

    def observed(self):
        with mock.patch("urllib.request.build_opener", return_value=self.api._client):
            return self.api.observe(self.fixture.policy, DEPLOYMENT)

    def test_exact_immutable_worker_catalog_only_and_detached_projection(self):
        held = self.reader.read()
        self.assertEqual(held, [{"id": base_pins()["version"], "prefix": base_pins()["version"][:8]}])
        request = self.api._client.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/workers/" + self.fixture.policy["id"] + "/versions?per_page=50&page=1")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("private-test-token", str(held))
        for number in (True, None, 0, 11, "1", "https://foreign.invalid"):
            self.assertRaises(Refusal, self.reader.page, number)
        self.assertEqual(self.api._client.open.call_count, 1)

    def test_explicit_domain_base_and_reader_custody_required_before_io(self):
        for token in (None, "", "bad token", "bad\nvalue"):
            self.assertRaises(Refusal, versions.Reader, token, self.fixture.registered, self.fixture.policy)
        for policy in ({key: self.fixture.policy[key] for key in ("id", "subdomain")}, dict(self.fixture.policy, domain=None), dict(self.fixture.policy, base=None)):
            with mock.patch("urllib.request.build_opener") as opener:
                self.assertRaises(Refusal, versions.Reader, "token", self.fixture.registered, policy)
                opener.assert_not_called()

    def test_complete_two_page_catalog_is_not_truncated(self):
        values = [self.item(base_pins()["version"])] + [self.item(f"{number:08x}-2222-3333-4444-555555555555") for number in range(1, 51)]
        def received(request, timeout):
            page = int(request.full_url.rsplit("=", 1)[1])
            return Response(json.dumps(self.reply(values[(page - 1) * 50:page * 50], page, 51)).encode(), request.full_url)
        self.api._client.open.side_effect = received
        self.assertEqual(len(self.reader.read()), 51)
        self.assertEqual(self.api._client.open.call_count, 2)

    def test_pagination_missing_truncated_moved_or_oversized_refuses(self):
        values = [self.item(base_pins()["version"])] + [self.item(f"{number:08x}-2222-3333-4444-555555555555") for number in range(1, 50)]
        for mode in ("truncated", "missing", "width", "oversized", "movement", "duplicate"):
            def received(request, timeout):
                page = int(request.full_url.rsplit("=", 1)[1])
                held = self.reply(values if page == 1 else [values[0]], page, 51)
                if mode == "missing":
                    del held["result_info"]
                elif mode == "width":
                    held["result_info"]["per_page"] = 100
                elif mode == "oversized":
                    held["result_info"]["total_count"] = 101
                elif mode == "truncated":
                    held["result"] = held["result"][:-1]
                elif mode == "movement" and page == 2:
                    held["result_info"]["total_count"] = 52
                return Response(json.dumps(held).encode(), request.full_url)
            self.api._client.open.side_effect = received
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                self.reader.read()

    def test_worker_version_urls_and_full_identity_are_exact(self):
        valid = self.item(base_pins()["version"])
        for urls in (None, [], valid["urls"][:1], valid["urls"] * 2, [valid["urls"][0], "https://foreign.invalid"],
                     [valid["urls"][0], valid["urls"][1].replace(".lane.", "-preview.lane.")]):
            self.assertRaises(Refusal, versions.project, dict(valid, urls=urls), self.parent)
        for requested in (None, True, "latest", valid["id"][:8], "ABCDEFAB-2222-3333-4444-555555555555"):
            self.assertRaises(Refusal, versions.project, dict(valid, id=requested), self.parent)
        held = versions.project(dict(valid, private="hidden"), self.parent)
        self.assertEqual(held, {"id": valid["id"], "prefix": valid["id"][:8]})

    def test_transport_errors_redirect_transformation_and_bad_json_refuse(self):
        valid = self.reply([self.item(base_pins()["version"])])
        for mode in ("http", "redirect", "gzip", "errors", "missing-errors", "duplicate-json", "oversized"):
            def received(request, timeout):
                if mode == "http":
                    raise urllib.error.HTTPError(request.full_url, 404, "private", {}, None)
                held = copy.deepcopy(valid)
                if mode == "errors":
                    held["errors"] = [{"message": "private"}]
                elif mode == "missing-errors":
                    del held["errors"]
                body = b'{"success":true,"success":true}' if mode == "duplicate-json" else json.dumps(held).encode()
                if mode == "oversized":
                    body = b"x" * 1048577
                response = Response(body, "https://foreign.invalid" if mode == "redirect" else request.full_url)
                if mode == "gzip":
                    response.headers["Content-Encoding"] = "gzip"
                return response
            self.api._client.open.reset_mock()
            self.api._client.open.side_effect = received
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                self.reader.read()
            self.assertEqual(self.api._client.open.call_count, 1)

    def test_private_qualification_requires_complete_base_and_cross_category_uniqueness(self):
        held = self.observed()
        observed, deployed = held["status"], held["deployment"]
        for values in (None, [], held["versions"] * 2, [{"id": DEPLOYMENT, "prefix": DEPLOYMENT[:8]}]):
            self.assertRaises(Refusal, routing.checked, dict(held, versions=values), observed, deployed)
        missing = dict(held)
        del missing["versions"]
        self.assertRaises(Refusal, routing.checked, missing, observed, deployed)
        for requested in (DEPLOYMENT, DEPLOYMENT[:8] + "-aaaa-bbbb-cccc-000000000001"):
            value = dict(held, versions=held["versions"] + [{"id": requested, "prefix": requested[:8]}])
            self.assertRaisesRegex(Refusal, "ambiguous", routing.checked, value, observed, deployed)
        altered = copy.deepcopy(observed)
        altered["previews"].append({"id": "foreign-id", "name": base_pins()["version"][:8], "parent": self.parent["id"]})
        self.assertRaisesRegex(Refusal, "ambiguous", routing.checked, qualification(altered, deployed), altered, deployed)
        for value in (None, {}, dict(held["versions"][0], private=True), dict(held["versions"][0], prefix="ffffffff")):
            self.assertRaises(Refusal, versions.checked, [value], self.parent)

    def test_worker_catalog_drift_and_legacy_url_spelling_stop_exact_observation(self):
        original = self.api._client.open.side_effect
        for mode in ("movement", "old-spelling", "version-collision", "missing-base"):
            count = []
            def received(request, timeout):
                with original(request, timeout) as response:
                    held = json.load(response)
                if "/versions?" in request.full_url:
                    count.append(request.full_url)
                    if mode == "movement" and len(count) == 2:
                        held["result"].append(self.item("aaaaaaaa-2222-3333-4444-555555555555"))
                        held["result_info"].update(count=2, total_count=2)
                    elif mode == "version-collision":
                        held["result"].append(self.item(DEPLOYMENT))
                        held["result_info"].update(count=2, total_count=2)
                    elif mode == "missing-base":
                        held["result"] = []
                        held["result_info"].update(count=0, total_count=0)
                elif mode == "old-spelling" and "/deployments" in request.full_url:
                    for item in held["result"] if isinstance(held["result"], list) else [held["result"]]:
                        if item.get("urls"):
                            item["urls"][1] = item["urls"][1].replace(".lane.", "-preview.lane.")
                return Response(json.dumps(held).encode(), request.full_url)
            self.api._client.open.side_effect = received
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                self.observed()

    def test_actual_source_addressed_sample_qualifies_without_logical_aliases(self):
        resource = resources.read_json("identity/deployment-observation.json")
        sample = resource["routing"]
        before = copy.deepcopy(sample)
        pins = copy.deepcopy(self.fixture.policy["domain"])
        domain = resource["domain_binding"]["domain"]
        pins["account"] = resource["domain_binding"]["provenance"]["domain"]["path"].split("/")[2]
        pins["worker"] = {key: sample["worker"][key] for key in ("id", "name")}
        pins["domain"] = {key: domain[key] for key in ("id", "hostname", "zone_id", "zone_name")}
        pins["domain"]["certificate_id"] = domain["cert_id"]
        synthetic = registered(operation("review"))
        synthetic["mapping"]["account"] = pins["account"]
        policy = {"id": pins["worker"]["id"], "subdomain": "862457174", "domain": pins,
                  "base": dict(base_pins(), version=sample["versions"][0]["id"])}
        parent = context.projected(policy, mapping.selected(synthetic))
        previews = sorted([inventory.project(item, parent) for item in sample["previews"]], key=lambda item: item["name"])
        selected = next(item for item in previews if item["name"] == "preview-design")
        deployed = observation.endpoint(sample["deployment"], selected, sample["deployment"]["id"])
        observed = {"parent": parent, "previews": previews, "preview": selected, "deployments": [deployed]}
        catalogs = []
        for preview in previews:
            raw = next(item for item in sample["catalogs"] if item["id"] == preview["id"])
            catalogs.append({"preview": preview, "deployments": [routing.project(item, preview, parent) for item in raw["deployments"]]})
        held = {"status": observed, "deployment": deployed, "catalogs": catalogs,
                "versions": [versions.project(item, parent) for item in sample["versions"]]}
        self.assertEqual(routing.checked(held, observed, deployed), "https://7f201597.lane.crest.perish.uk")
        self.assertEqual(sample["provenance"]["issue"], "PerishLab/wharf#238")
        self.assertTrue(all(item["policy"] == "accepted" and item["bytes_match"] for item in sample["public"]["resources"]))
        self.assertEqual(synthetic["declaration"]["target"]["lane"], "review")
        self.assertRaises(Refusal, operation, "preview-design")
        self.assertEqual(sample, before)
