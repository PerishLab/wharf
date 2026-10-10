import copy
import json
import unittest
import urllib.error
from unittest import mock

from lib.process.cfworker import mapping, observation, reader
from lib.refusal import Refusal
from tests.lib.process.test_cfworker import DEPLOYMENT, Response
from tests.lib.store.test_lane import operation, registered


class Observation(unittest.TestCase):
    def setUp(self):
        self.registered = registered(operation())
        self.selected = mapping.selected(self.registered)
        self.policy = {"id": "e8f70fdbc8b1fb0b8ddb1af166186758", "subdomain": "perishlab"}
        self.parent = {"id": self.policy["id"], "name": "crest-development", "subdomain": {"enabled": False, "previews_enabled": True, "preview_url_suffix": "-crest-development.perishlab.workers.dev"}, "deployed_on": None, "previews_base_config": None, "references": dict.fromkeys(observation.REFERENCES, [])}
        self.preview = {"id": "preview-id", "name": "preview", "slug": "preview", "urls": ["https://preview-crest-development.perishlab.workers.dev"]}
        self.deployment = {"id": DEPLOYMENT, "preview_id": "preview-id", "preview_name": "preview"}

    def client(self):
        api = reader.Reader("private-test-token", self.registered)
        api._client = mock.Mock()
        replies = iter([self.parent, self.preview, self.deployment, self.parent])
        api._client.open.side_effect = lambda request, timeout: Response(json.dumps({"success": True, "errors": [], "result": next(replies)}).encode(), request.full_url)
        return api

    def test_default_and_two_keyed_identities(self):
        for lane in ("preview", "preview.design", "preview.review"):
            selected = mapping.selected(registered(operation(lane)))
            parent = observation.parent(self.parent, selected, self.policy)
            value = dict(self.preview, name=selected["name"], slug=selected["name"], urls=["https://" + selected["name"] + parent["suffix"]])
            preview = observation.preview(value, selected, parent)
            deployment = observation.deployment(dict(self.deployment, preview_name=selected["name"]), preview, DEPLOYMENT)
            self.assertEqual(deployment, {"id": DEPLOYMENT, "preview": "preview-id", "parent": self.policy["id"]})

    def test_missing_and_drifted_parent_refuse(self):
        changes = [{"id": "another"}, {"name": "production"}, {"deployed_on": "2026-10-10T00:00:00Z"}, {"previews_base_config": {"env": {"PRIVATE": "never"}}}, {"references": {}}, {"references": dict(self.parent["references"], domains=["production"])}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Refusal):
                observation.parent(dict(self.parent, **change), self.selected, self.policy)

    def test_missing_critical_parent_fields_refuse(self):
        for key in ("id", "name", "deployed_on", "references", "subdomain"):
            value = copy.deepcopy(self.parent)
            del value[key]
            with self.subTest(key=key), self.assertRaises(Refusal):
                observation.parent(value, self.selected, self.policy)

    def test_routing_policy_drift_refuses(self):
        for change in ({"enabled": True}, {"enabled": 0}, {"previews_enabled": False}, {"previews_enabled": 1}, {"preview_url_suffix": "-crest-development.attacker.workers.dev"}):
            value = dict(self.parent, subdomain=dict(self.parent["subdomain"], **change))
            with self.subTest(change=change), self.assertRaises(Refusal):
                observation.parent(value, self.selected, self.policy)

    def test_prepared_policy_invalid_before_network(self):
        for policy in (None, {}, {"id": "../parent", "subdomain": "perishlab"}, dict(self.policy, subdomain="a.b"), dict(self.policy, secret="never")):
            api = self.client()
            with self.assertRaises(Refusal):
                api.observe(policy, DEPLOYMENT)
            api._client.open.assert_not_called()

    def test_preview_identity_slug_and_native_address_refuse(self):
        parent = observation.parent(self.parent, self.selected, self.policy)
        for change in ({"id": "../another"}, {"name": "another"}, {"slug": "another"}, {"urls": []}, {"urls": [self.preview["urls"][0] + "/"]}, {"urls": ["https://preview-crest-development.attacker.workers.dev"]}, {"urls": [self.preview["urls"][0], "https://attacker.invalid"]}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                observation.preview(dict(self.preview, **change), self.selected, parent)

    def test_deployment_identity_never_uses_latest_or_annotations(self):
        parent = observation.parent(self.parent, self.selected, self.policy)
        preview = observation.preview(self.preview, self.selected, parent)
        for change in ({"id": "latest"}, {"preview_id": "another"}, {"preview_name": "another"}, {"id": DEPLOYMENT.upper()}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                observation.deployment(dict(self.deployment, **change), preview, DEPLOYMENT)
        for requested in (None, True, "latest", "../deployment"):
            with self.assertRaises(Refusal):
                observation.deployment(self.deployment, preview, requested)

    def test_projection_omits_private_fields_and_is_detached(self):
        for value in (self.parent, self.preview, self.deployment):
            value["env"] = {"PRIVATE": "never-publish"}
            value["annotations"] = {"workers/commit_sha": "not-content-proof"}
        api = self.client()
        held = api.observe(self.policy, DEPLOYMENT)
        self.assertEqual(set(held), {"parent", "preview", "deployment"})
        self.assertNotIn("never-publish", json.dumps(held))
        self.assertNotIn("not-content-proof", json.dumps(held))
        self.assertNotIn("verified", json.dumps(held))
        self.assertEqual(api._client.open.call_count, 4)
        self.parent["name"] = "changed"
        self.assertEqual(held["parent"]["name"], "crest-development")

    def test_failure_at_any_read_never_becomes_absence(self):
        for step in range(4):
            api = self.client()
            original = api._client.open.side_effect
            calls = []

            def read(request, timeout):
                calls.append(request)
                if len(calls) == step + 1:
                    raise urllib.error.HTTPError("private-test-token", 404, "private-test-token", {}, None)
                return original(request, timeout)

            api._client.open.side_effect = read
            with self.assertRaises(Refusal) as raised:
                api.observe(self.policy, DEPLOYMENT)
            self.assertEqual(len(calls), step + 1)
            self.assertNotIn("private-test-token", str(raised.exception))

    def test_parent_reread_drift_and_policy_mutation_refuse(self):
        api = self.client()
        original = api._client.open.side_effect
        calls = []

        def read(request, timeout):
            calls.append(request)
            if len(calls) == 4:
                self.policy["id"] = "replacement"
                self.parent["id"] = "replacement"
            return original(request, timeout)

        api._client.open.side_effect = read
        with self.assertRaises(Refusal):
            api.observe(self.policy, DEPLOYMENT)

    def test_malformed_objects_and_missing_identity_fields_refuse(self):
        parent = observation.parent(self.parent, self.selected, self.policy)
        preview = observation.preview(self.preview, self.selected, parent)
        for value in (None, [], True, "private"):
            for invoke in (lambda: observation.parent(value, self.selected, self.policy), lambda: observation.preview(value, self.selected, parent), lambda: observation.deployment(value, preview, DEPLOYMENT)):
                with self.assertRaises(Refusal):
                    invoke()
        for original, invoke in ((self.preview, lambda value: observation.preview(value, self.selected, parent)), (self.deployment, lambda value: observation.deployment(value, preview, DEPLOYMENT))):
            for key in original:
                value = dict(original)
                del value[key]
                with self.subTest(key=key), self.assertRaises(Refusal):
                    invoke(value)


if __name__ == "__main__":
    unittest.main()
