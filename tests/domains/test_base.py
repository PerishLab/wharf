import base64
import copy
import hashlib
import json
import unittest

from lib.process.domains import base
from lib.refusal import Refusal


VERSION = "11111111-2222-3333-4444-555555555555"
DEPLOYMENT = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def pins():
    return {"version": VERSION, "deployment": DEPLOYMENT,
            "source": hashlib.sha256(base.material()["source"].encode()).hexdigest()}


def version():
    held = base.material()
    return {"id": VERSION, "main_module": held["main_module"],
            "compatibility_date": held["compatibility_date"], "compatibility_flags": [],
            "bindings": [], "assets": None, "tail_consumers": [],
            "modules": [{"name": held["main_module"], "content_type": "application/javascript+module",
                         "content_base64": base64.b64encode(held["source"].encode()).decode("ascii")}]}


def deployment():
    return {"id": DEPLOYMENT, "strategy": "percentage",
            "versions": [{"version_id": VERSION, "percentage": 100}]}


def observed():
    return {"version": version(), "deployment": deployment(),
            "current": {"deployments": [deployment()]}}


class BaseIdentity(unittest.TestCase):
    def test_exact_detached_pins_and_projection(self):
        value = observed()
        prepared = pins()
        before = copy.deepcopy(value)
        held = base.qualify(prepared, value)
        self.assertEqual(held, prepared)
        held["version"] = "changed"
        self.assertEqual(prepared, pins())
        self.assertEqual(before, value)

    def test_missing_unknown_malformed_and_oversized_pins_refuse(self):
        for value in (None, [], {}, dict(pins(), extra=True), dict(pins(), source="x" * 65536)):
            self.assertRaises(Refusal, base.prepared, value)
        for key in pins():
            value = pins()
            del value[key]
            self.assertRaises(Refusal, base.prepared, value)
            for invalid in (None, True, [], "", "foreign", "../version", "latest"):
                self.assertRaises(Refusal, base.prepared, dict(pins(), **{key: invalid}))

    def test_full_uuid_and_exact_inert_source_required(self):
        for key in ("version", "deployment"):
            for value in (VERSION[:8], DEPLOYMENT.upper(), VERSION + "?x=1", "https://other.invalid/" + VERSION):
                self.assertRaises(Refusal, base.prepared, dict(pins(), **{key: value}))
        self.assertRaises(Refusal, base.prepared, dict(pins(), source="f" * 64))

    def test_all_observation_components_are_explicit_and_bounded(self):
        for value in (None, [], {}, dict(observed(), permit=True),
                      dict(observed(), version=dict(version(), private="x" * 65536))):
            self.assertRaises(Refusal, base.qualify, pins(), value)
        for key in observed():
            value = observed()
            del value[key]
            self.assertRaises(Refusal, base.qualify, pins(), value)

    def test_version_identity_and_complete_module_bytes_are_exact(self):
        for key in ("id", "main_module", "compatibility_date", "compatibility_flags", "modules", "bindings"):
            value = version()
            del value[key]
            self.assertRaises(Refusal, base.version, pins(), value)
        for change in ({"id": DEPLOYMENT}, {"main_module": "foreign.js"}, {"compatibility_date": "2000-01-01"},
                       {"compatibility_flags": ["nodejs_compat"]}, {"modules": []}, {"modules": None}):
            self.assertRaises(Refusal, base.version, pins(), dict(version(), **change))

    def test_each_module_field_and_extra_modules_refuse(self):
        for key in version()["modules"][0]:
            for mode in ("missing", "foreign"):
                value = version()
                if mode == "missing":
                    del value["modules"][0][key]
                else:
                    value["modules"][0][key] = "foreign"
                self.assertRaises(Refusal, base.version, pins(), value)
        value = version()
        value["modules"].append(copy.deepcopy(value["modules"][0]))
        self.assertRaises(Refusal, base.version, pins(), value)
        value = version()
        value["modules"][0]["private"] = "hidden"
        self.assertRaises(Refusal, base.version, pins(), value)

    def test_etag_is_not_source_authority(self):
        value = version()
        value["etag"] = pins()["source"]
        value["modules"][0]["content_base64"] = base64.b64encode(b"export default {};").decode()
        self.assertRaises(Refusal, base.version, pins(), value)

    def test_bindings_assets_and_tail_consumers_refuse(self):
        for change in ({"bindings": None}, {"bindings": {}}, {"bindings": [{"type": "plain_text", "name": "PRIVATE", "text": "hidden"}]},
                       {"assets": {}}, {"assets": {"jwt": "private"}}, {"tail_consumers": None},
                       {"tail_consumers": [{"service": "foreign"}]}):
            self.assertRaises(Refusal, base.version, pins(), dict(version(), **change))

    def test_deployment_identity_strategy_and_single_version_bind(self):
        for key in deployment():
            value = deployment()
            del value[key]
            self.assertRaises(Refusal, base.deployment, pins(), value)
        for change in ({"id": VERSION}, {"strategy": "gradual"}, {"versions": []},
                       {"versions": None}, {"versions": [deployment()["versions"][0]] * 2}):
            self.assertRaises(Refusal, base.deployment, pins(), dict(deployment(), **change))

    def test_percentage_and_version_cannot_be_coerced_or_split(self):
        for percentage in (None, False, True, "100", 0, 99.9, 101):
            value = deployment()
            value["versions"][0]["percentage"] = percentage
            self.assertRaises(Refusal, base.deployment, pins(), value)
        for row in ({"version_id": DEPLOYMENT, "percentage": 100},
                    {"version_id": VERSION}, {"version_id": VERSION, "percentage": 100, "extra": True}):
            self.assertRaises(Refusal, base.deployment, pins(), dict(deployment(), versions=[row]))
        self.assertEqual(base.deployment(pins(), dict(deployment(), versions=[{"version_id": VERSION, "percentage": 100.0}])), pins())

    def test_current_must_select_exact_deployment_not_historical_match(self):
        for current in (None, [], {}, {"deployments": []}, {"deployments": [deployment()] * 101},
                        {"deployments": [deployment()], "extra": True}):
            self.assertRaises(Refusal, base.qualify, pins(), dict(observed(), current=current))
        current = {"deployments": [dict(deployment(), id=VERSION), deployment()]}
        self.assertRaises(Refusal, base.qualify, pins(), dict(observed(), current=current))
        current = {"deployments": [deployment(), dict(deployment(), id=VERSION)]}
        self.assertEqual(base.qualify(pins(), dict(observed(), current=current)), pins())

    def test_current_version_drift_refuses(self):
        value = observed()
        value["current"]["deployments"][0]["versions"][0]["version_id"] = DEPLOYMENT
        self.assertRaises(Refusal, base.qualify, pins(), value)

    def test_private_provider_metadata_never_enters_projection(self):
        value = observed()
        value["version"]["annotations"] = {"private": "hidden"}
        value["deployment"]["author_email"] = "private@example.invalid"
        value["current"]["deployments"][0]["private"] = "hidden"
        self.assertEqual(base.qualify(pins(), value), pins())
        self.assertNotIn("private", json.dumps(base.qualify(pins(), value)))

    def test_material_is_detached_and_uses_no_product_inputs(self):
        original = base.material()
        mutated = base.material()
        mutated["source"] = "changed"
        self.assertEqual(base.material(), original)
        self.assertEqual(set(original), {"source", "main_module", "compatibility_date", "compatibility_flags"})

    def test_fixed_source_digest_and_runtime_material(self):
        self.assertEqual(pins()["source"], "aa3a4527167d225a89a9cbc0a3f5f17d90c40d83a5f300bd80c10648a64cdcda")
        self.assertEqual(base.material()["main_module"], "base.mjs")
        self.assertEqual(base.material()["compatibility_date"], "2026-10-19")
        self.assertEqual(base.material()["compatibility_flags"], [])

