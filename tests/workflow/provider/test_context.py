import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from lib.process.domains import reader
from lib.process.lane import provider
from lib.refusal import Refusal
from tests.domains import fixtures
from tests.workflow.provider import test_deployment, test_disposal, test_provider


class Context(unittest.TestCase):
    def setUp(self):
        self.fixture, self.api = fixtures.provider()
        self.env = {"WHARF_LANE_PARENT_ID": self.fixture.policy["id"], "WHARF_LANE_SUBDOMAIN": self.fixture.policy["subdomain"],
                    "WHARF_LANE_DOMAIN_PINS": json.dumps(self.fixture.policy["domain"])}

    def test_trusted_pins_select_independently_read_domain_context(self):
        policy = provider.policy(self.env, self.fixture.registered)
        with mock.patch.object(reader.urllib.request, "build_opener", return_value=self.api._client):
            observed = self.api.status(policy)
        self.assertEqual(observed["parent"]["domain"], self.fixture.policy["domain"])
        calls = [call.args[0] for call in self.api._client.open.call_args_list]
        self.assertEqual(sum("/workers/domains/domain-identity" in item.full_url for item in calls), 2)
        self.assertTrue(all(item.get_method() == "GET" for item in calls))
        policy["domain"]["app"] = "changed"
        self.assertEqual(provider.policy(self.env, self.fixture.registered), self.fixture.policy)

    def test_missing_malformed_duplicate_oversized_and_incomplete_pins_refuse(self):
        for value in (None, "", False, {}, "[]", "null", "{", '{"app":"crest","app":"crest"}',
                      " " * 65537, json.dumps(dict(self.fixture.policy["domain"], extra=True))):
            env = dict(self.env, WHARF_LANE_DOMAIN_PINS=value)
            with self.subTest(value=str(value)[:40]), self.assertRaises(Refusal):
                provider.policy(env, self.fixture.registered)
        for key in self.fixture.policy["domain"]:
            pins = copy.deepcopy(self.fixture.policy["domain"])
            del pins[key]
            with self.subTest(missing=key), self.assertRaises(Refusal):
                provider.policy(dict(self.env, WHARF_LANE_DOMAIN_PINS=json.dumps(pins)), self.fixture.registered)
        self.api._client.open.assert_not_called()

    def test_domain_pins_do_not_supply_missing_or_foreign_parent_policy(self):
        for key in ("WHARF_LANE_PARENT_ID", "WHARF_LANE_SUBDOMAIN"):
            env = dict(self.env)
            del env[key]
            self.assertRaises(Refusal, provider.policy, env, self.fixture.registered)
        self.assertRaises(Refusal, provider.policy, dict(self.env, WHARF_LANE_PARENT_ID="foreign"), self.fixture.registered)
        self.assertRaises(Refusal, provider.policy, dict(self.env, WHARF_LANE_SUBDOMAIN="../foreign"), self.fixture.registered)
        self.api._client.open.assert_not_called()

    def test_foreign_repository_app_account_parent_domain_zone_certificate_refuse(self):
        changes = [("repository", "PerishLab/other"), ("app", "other"), ("account", "f" * 32),
                   ("worker", {"id": "foreign", "name": "crest-development"})]
        for key, value in changes:
            pins = dict(self.fixture.policy["domain"], **{key: value})
            self.assertRaises(Refusal, provider.policy, dict(self.env, WHARF_LANE_DOMAIN_PINS=json.dumps(pins)), self.fixture.registered)
        for key, value in (("hostname", "lane.other.perish.uk"), ("zone_id", "invalid"), ("certificate_id", "invalid")):
            pins = copy.deepcopy(self.fixture.policy["domain"])
            pins["domain"][key] = value
            self.assertRaises(Refusal, provider.policy, dict(self.env, WHARF_LANE_DOMAIN_PINS=json.dumps(pins)), self.fixture.registered)
        self.api._client.open.assert_not_called()

    def test_observer_missing_pins_never_reads_provider(self):
        source = test_provider.Provider()
        self.addCleanup(source.doCleanups)
        _, _, store, bucket, env, held = source.present()
        del env["WHARF_LANE_DOMAIN_PINS"]
        api = mock.Mock()
        with self.assertRaises(Refusal):
            source.invoke(store, bucket, held, (env, api))
        api.status.assert_not_called()
        self.api._client.open.assert_not_called()

    def test_deployment_rechecks_trusted_pins_against_preflight_before_writes(self):
        source = test_deployment.Deployment()
        self.addCleanup(source.doCleanups)
        fixture = source.prepared()
        original = fixture["env"]["WHARF_LANE_DOMAIN_PINS"]
        for mode in ("missing", "changed", "native-only"):
            pins = json.loads(original)
            pins["domain"]["id"] = "new-domain"
            fixture["env"]["WHARF_LANE_DOMAIN_PINS"] = "" if mode == "missing" else json.dumps(pins)
            if mode == "native-only":
                fixture["env"]["WHARF_LANE_DOMAIN_PINS"] = original
                del fixture["current"]["parent"]["domain"]
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                source.invoke(fixture)
            fixture["transport"].open.assert_not_called()
            fixture["reads"].open.assert_not_called()
        self.assertEqual(list(Path(fixture["env"]["RUNNER_TEMP"]).iterdir()), [])

    def test_disposal_rechecks_trusted_pins_before_single_delete(self):
        source = test_disposal.Disposal()
        self.addCleanup(source.doCleanups)
        store, env, held, api = source.prepared()
        original = env["WHARF_LANE_DOMAIN_PINS"]
        for value in ("", json.dumps(dict(json.loads(original), account="f" * 32))):
            env["WHARF_LANE_DOMAIN_PINS"] = value
            transport = source.transport()
            with self.assertRaises(Refusal):
                source.invoke(store, env, held, (api, transport))
            transport.open.assert_not_called()
        api.status.assert_not_called()
        self.assertEqual(list(Path(env["RUNNER_TEMP"]).iterdir()), [])

    def test_workflow_context_comes_only_from_trusted_vars_not_dispatch_or_guests(self):
        root = Path(provider.__file__).resolve().parents[3]
        for name in ("observe", "deploy", "dispose"):
            body = (root / f".github/workflows/{name}.yml").read_text()
            self.assertEqual(body.count("${{ vars.WHARF_LANE_DOMAIN_PINS }}"), 1)
            self.assertNotIn("inputs.domain-pins", body)
            self.assertNotIn("secrets: inherit", body)
        for name in ("deployment", "disposal"):
            body = (root / f".github/actions/lane-{name}/action.yml").read_text()
            self.assertIn("WHARF_LANE_DOMAIN_PINS: ${{ inputs.domain-pins }}", body)
            self.assertIn("domain-pins:\n    required: true", body)
        for name in ("intake", "evidence"):
            self.assertNotIn("WHARF_LANE_DOMAIN_PINS", (root / f".github/workflows/{name}.yml").read_text())
        self.assertNotIn("WHARF_LANE_DOMAIN_PINS", (root / ".github/actions/preview-source/action.yml").read_text())


if __name__ == "__main__":
    unittest.main()
