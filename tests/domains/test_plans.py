import copy
import unittest
from unittest import mock

from lib.process.cfworker import apply, create, discard, mapping, reader, writer
from lib.process.domains import context
from lib.refusal import Refusal
from lib.store.lane import state
from tests.cfworker import test_inspection, test_readback
from tests.domains import fixtures
from tests.domains.test_routing import qualification
from tests.lib.process import test_cfworker_discard


class Plans(unittest.TestCase):
    def setUp(self):
        self.base = test_readback.Readback()
        self.base.setUp()
        self.policy = fixtures.policy(self.base.plan["policy"])
        self.registered = self.base.plan["apply"]["registration"]
        self.parent = context.projected(self.policy, mapping.selected(self.registered))
        before = dict(self.base.fixture.before, parent=self.parent)
        self.plan = apply.deployment_prepare(self.base.plan["apply"], before, self.policy)
        self.after = dict(before, deployments=[self.base.deployed])
        self.base.plan, self.base.after = self.plan, self.after
        self.base.origin = "https://12345678-preview.lane.crest.perish.uk"

    def test_exact_immutable_domain_content_settles_without_changing_public_operation(self):
        api = self.base.client()
        proof = self.base.invoke(api)
        operation = apply.settle(self.plan, self.after, proof, "idle")
        self.assertEqual(proof["address"], self.base.origin)
        self.assertEqual(proof["routing"]["status"]["parent"]["domain"], self.policy["domain"])
        self.assertEqual(set(operation), {"request", "assessment", "outcome"})
        self.assertEqual(operation["outcome"]["material"]["artifact"], self.plan["apply"]["artifact"])
        self.assertNotIn("lane.crest.perish.uk", str(operation))
        self.assertTrue(all(call.args[0].full_url.startswith(self.base.origin + "/") for call in api._client.open.call_args_list))
        self.assertFalse(any(call.args[0].get_header("Authorization") for call in api._client.open.call_args_list))

    def test_current_domain_read_is_idempotent_and_never_immutable_settlement(self):
        api = self.base.client()
        api.current().current()
        self.base.origin = "https://preview.lane.crest.perish.uk"
        proof = self.base.invoke(api)
        self.assertEqual(proof["address"], self.base.origin)
        self.assertRaises(Refusal, apply.settle, self.plan, self.after, proof, "idle")

    def test_domain_policy_or_projection_cannot_disappear_or_substitute_pins(self):
        for mode in ("policy-missing", "status-missing", "changed-certificate", "changed-account"):
            plan = copy.deepcopy(self.plan)
            if mode == "policy-missing":
                del plan["policy"]["domain"]
            elif mode == "status-missing":
                del plan["before"]["parent"]["domain"]
            elif mode == "changed-certificate":
                plan["before"]["parent"]["domain"]["domain"]["certificate_id"] = "foreign"
            else:
                plan["policy"]["domain"]["account"] = "f" * 32
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                apply.deployment_readable(plan)
        held = apply.deployment_readable(self.plan)
        held["policy"]["domain"]["domain"]["id"] = "changed"
        self.assertEqual(self.plan["policy"], self.policy)

    def test_security_indexing_media_and_exact_bytes_remain_required_without_fallback(self):
        for header, value in (("X-Robots-Tag", "noindex"), ("Content-Security-Policy", "default-src *"),
                              ("Cache-Control", "public"), ("X-Content-Type-Options", "wrong"),
                              ("Content-Type", "text/plain"), ("Content-Encoding", "gzip")):
            api = self.base.client()
            def received(request, timeout):
                response = self.base.received(request, timeout)
                del response.headers[header]
                response.headers[header] = value
                return response
            api._client.open.side_effect = received
            with self.subTest(header=header), self.assertRaises(Refusal):
                self.base.invoke(api)
            self.assertEqual(api._client.open.call_count, 1)
        api = self.base.client()
        def changed(request, timeout):
            response = self.base.received(request, timeout)
            response.seek(0)
            response.write(b"changed")
            response.seek(0)
            return response
        api._client.open.side_effect = changed
        self.assertRaises(Refusal, self.base.invoke, api)
        self.assertEqual(api._client.open.call_count, 1)

    def test_missing_domain_namespace_or_changed_prefix_refuses_before_content_io(self):
        for mode in ("domain", "prefix", "catalog", "uuid"):
            routed = qualification(self.after, self.base.deployed)
            if mode == "domain":
                del routed["status"]["parent"]["domain"]
            elif mode == "prefix":
                routed["catalogs"][0]["deployments"][0]["prefix"] = "ffffffff"
            elif mode == "uuid":
                routed["catalogs"][0]["deployments"][0]["prefix"] = self.base.deployed["id"]
            else:
                routed["catalogs"].pop()
            with mock.patch("urllib.request.build_opener") as opener:
                self.assertRaises(Refusal, reader.Readback, self.plan, self.after, routed)
                opener.assert_not_called()

    def test_creation_and_deployment_plans_retain_domain_without_domain_writers(self):
        before = {key: self.plan["before"][key] for key in ("parent", "previews", "preview")}
        selected = before["preview"]
        before.update(preview=None, previews=[item for item in before["previews"] if item != selected])
        held = create.prepare(self.plan["apply"], before, self.policy)
        after = {key: self.plan["before"][key] for key in ("parent", "previews", "preview")}
        self.assertEqual(create.confirmed(held, after, selected), selected)
        self.assertEqual(held["policy"]["domain"], self.policy["domain"])
        self.base.fixture.plan, self.base.fixture.before = self.plan, self.plan["before"]
        api = self.base.fixture.client()
        result = self.base.fixture.invoke(api)
        self.assertTrue(result["acknowledged"])
        self.assertIn("/previews/selected-id/deployments?ignore_defaults=true", api._client.open.call_args.args[0].full_url)
        self.assertNotIn("/workers/domains", api._client.open.call_args.args[0].full_url)

    def test_unknown_apply_preserves_domain_context_and_still_refuses_new_writer(self):
        current = self.plan["apply"]["current"]
        unknown = apply.unknown(self.plan, "unverified")
        current = state.finished(current, unknown, self.registered)
        resumed = apply.resume(self.plan, current)
        self.assertEqual(resumed["policy"], self.policy)
        self.assertEqual(resumed["before"]["parent"], self.parent)
        self.assertRaises(Refusal, writer.Deployer, "private-test-token", resumed, "a.b.c")
        self.assertEqual(apply.candidate(resumed, self.after), self.base.deployed)

    def test_dispose_plan_and_absent_tombstone_retain_private_domain_context(self):
        fixture = test_cfworker_discard.Discard()
        fixture.setUp()
        policy = fixtures.policy(fixture.policy)
        registered = fixture.plan["registration"]
        before = dict(fixture.plan["before"], parent=context.projected(policy, mapping.selected(registered)))
        plan = discard.prepare(registered, fixture.plan["current"], before, policy)
        after = fixture.absent(plan)
        operation = discard.settle(plan, after, "idle")
        self.assertEqual(plan["policy"], policy)
        self.assertEqual(operation["outcome"]["material"], {"state": "absent"})
        del after["parent"]["domain"]
        self.assertRaises(Refusal, discard.settle, plan, after, "idle")

    def test_inspection_uses_only_custom_current_domain_and_saved_bytes(self):
        fixture = test_inspection.Inspection()
        fixture.setUp()
        policy = fixtures.policy(fixture.plan["policy"])
        fixture.plan = reader.inspection(dict(fixture.plan, policy=policy))
        fixture.observed["parent"] = context.projected(policy, mapping.selected(fixture.registered))
        fixture.origin = "https://preview.lane.crest.perish.uk"
        api = fixture.client()
        operation = fixture.invoke(api)
        self.assertEqual(operation["outcome"]["outcome"], "applied")
        self.assertTrue(all(call.args[0].full_url.startswith(fixture.origin + "/") for call in api._client.open.call_args_list))
        del fixture.observed["parent"]["domain"]
        self.assertRaises(Refusal, reader.Inspection, fixture.plan, fixture.observed)
