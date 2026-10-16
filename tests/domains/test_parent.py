import copy
import unittest

from lib.process.cfworker import mapping, observation
from lib.process.domains import parent
from lib.refusal import Refusal
from tests.domains.test_domain import pins, reply
from tests.lib.store.test_lane import operation, registered


class DomainParent(unittest.TestCase):
    def setUp(self):
        self.registered = registered(operation("review"))
        self.pins = pins()
        self.policy = {"id": self.pins["worker"]["id"], "subdomain": "perishlab"}
        self.raw = {"id": self.policy["id"], "name": "crest-development", "deployed_on": None,
                    "previews_base_config": None,
                    "subdomain": {"enabled": False, "previews_enabled": True,
                                  "preview_url_suffix": "-crest-development.perishlab.workers.dev"},
                    "references": {key: [] for key in observation.REFERENCES}}
        self.raw["references"]["domains"] = [copy.deepcopy(self.pins["domain"])]
        self.value = {"policy": self.policy, "parent": self.raw, "domain": reply()}

    def qualify(self, value):
        return parent.qualify(self.registered, self.pins, value)

    def test_exact_join_is_detached_and_preserves_original_observations(self):
        before = copy.deepcopy(self.value)
        held = self.qualify(self.value)
        self.assertEqual(held, {"parent": {"id": self.policy["id"], "name": "crest-development",
                                          "account": "a" * 32, "suffix": "-crest-development.perishlab.workers.dev"},
                                "domain": reply()})
        self.assertEqual(before, self.value)
        held["domain"]["id"] = "changed"
        held["parent"]["id"] = "changed"
        self.assertEqual(self.pins, pins())
        self.assertEqual(self.qualify(self.value)["domain"], reply())
        self.assertNotIn("cert_id", self.raw["references"]["domains"][0])
        self.assertNotIn("certificate_id", self.qualify(self.value)["domain"])

    def test_ordinary_parent_observer_still_refuses_nonempty_references(self):
        selected = mapping.selected(self.registered)
        self.assertRaises(Refusal, observation.parent, self.raw, selected, self.policy)
        raw = copy.deepcopy(self.raw)
        raw["references"]["domains"] = []
        self.assertEqual(observation.parent(raw, selected, self.policy), self.qualify(self.value)["parent"])
        self.assertRaises(Refusal, self.qualify, dict(self.value, parent=raw))

    def test_reference_catalog_is_complete_and_has_exactly_one_pinned_domain(self):
        domain = self.pins["domain"]
        for references in (None, {}, [], dict(self.raw["references"], extra=[])):
            self.assertRaises(Refusal, self.qualify, dict(self.value, parent=dict(self.raw, references=references)))
        for domains in (None, {}, domain, [], [domain, domain], [domain, {}], [reply()], [dict(domain, enabled=False)]):
            raw = dict(self.raw, references=dict(self.raw["references"], domains=domains))
            with self.subTest(domains=domains), self.assertRaises(Refusal):
                self.qualify(dict(self.value, parent=raw))

    def test_every_domain_reference_identity_field_binds_exactly(self):
        for key in self.pins["domain"]:
            for mode in ("missing", "foreign", "null"):
                domain = copy.deepcopy(self.pins["domain"])
                if mode == "missing":
                    del domain[key]
                else:
                    domain[key] = None if mode == "null" else "foreign"
                raw = dict(self.raw, references=dict(self.raw["references"], domains=[domain]))
                with self.subTest(key=key, mode=mode), self.assertRaises(Refusal):
                    self.qualify(dict(self.value, parent=raw))

    def test_all_non_domain_references_remain_empty_and_present(self):
        for key in observation.REFERENCES - {"domains"}:
            for value in (None, {}, ["foreign"], [self.pins["domain"]]):
                raw = dict(self.raw, references=dict(self.raw["references"], **{key: value}))
                with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                    self.qualify(dict(self.value, parent=raw))
            raw = copy.deepcopy(self.raw)
            del raw["references"][key]
            self.assertRaises(Refusal, self.qualify, dict(self.value, parent=raw))

    def test_independent_domain_reply_cannot_be_replaced_by_parent_reference(self):
        for key in reply():
            for value in (None, "foreign"):
                with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                    self.qualify(dict(self.value, domain=dict(reply(), **{key: value})))
            domain = reply()
            del domain[key]
            self.assertRaises(Refusal, self.qualify, dict(self.value, domain=domain))
        for domain in (None, [], self.pins["domain"]):
            self.assertRaises(Refusal, self.qualify, dict(self.value, domain=domain))

    def test_preview_only_flags_require_literal_booleans(self):
        for key, values in (("enabled", (True, 0, 1, None, "false")),
                            ("previews_enabled", (False, 0, 1, None, "true"))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                    self.qualify(dict(self.value, domain=dict(reply(), **{key: value})))

    def test_immutable_worker_policy_and_parent_isolation_are_not_bypassed(self):
        for policy in ({"id": "other", "subdomain": "perishlab"}, dict(self.policy, extra=True),
                       dict(self.policy, subdomain="foreign"), None):
            self.assertRaises(Refusal, self.qualify, dict(self.value, policy=policy))
        for change in ({"id": "other"}, {"name": "production"}, {"deployed_on": "2026-10-16"},
                       {"previews_base_config": {"env": {"PRIVATE": "hidden"}}}, {"subdomain": None}):
            self.assertRaises(Refusal, self.qualify, dict(self.value, parent=dict(self.raw, **change)))
        for change in ({"enabled": True}, {"enabled": 0}, {"previews_enabled": False},
                       {"previews_enabled": 1}, {"preview_url_suffix": "-other.perishlab.workers.dev"}):
            raw = dict(self.raw, subdomain=dict(self.raw["subdomain"], **change))
            self.assertRaises(Refusal, self.qualify, dict(self.value, parent=raw))

    def test_account_registration_worker_and_app_pins_still_bind(self):
        for key, value in (("account", "c" * 32), ("repository", "PerishLab/other"), ("app", "other"),
                           ("worker", {"id": "foreign", "name": "crest-development"}),
                           ("worker", {"id": self.policy["id"], "name": "other"})):
            self.assertRaises(Refusal, parent.qualify, self.registered, dict(self.pins, **{key: value}), self.value)
        self.assertRaises(Refusal, parent.qualify, dict(self.registered, enabled=False), self.pins, self.value)

    def test_arbitrary_provider_metadata_never_enters_finite_projection(self):
        value = copy.deepcopy(self.value)
        value["parent"]["annotations"] = {"private": "hidden"}
        value["domain"]["annotations"] = {"private": "hidden"}
        held = self.qualify(value)
        self.assertNotIn("private", str(held))
        value["parent"]["name"] = "changed"
        value["domain"]["id"] = "changed"
        self.assertEqual(held, self.qualify(self.value))

    def test_bounded_exact_observation_envelope_refuses_unknown_inputs(self):
        for value in (None, [], {}, dict(self.value, credential="private"),
                      dict(self.value, parent=dict(self.raw, annotation="x" * 65536))):
            self.assertRaises(Refusal, self.qualify, value)
        for key in self.value:
            value = copy.deepcopy(self.value)
            del value[key]
            self.assertRaises(Refusal, self.qualify, value)
