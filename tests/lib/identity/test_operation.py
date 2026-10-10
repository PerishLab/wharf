import copy
import hashlib
import json
import unittest

from lib.content import resources
from lib.content.lane import codec
from lib.refusal import Refusal

FIXTURE = resources.read_json("identity/lane-operation-fixtures.json")
CASES = {case["id"]: case for case in FIXTURE["cases"]}


def example(name):
    return json.loads(CASES[name]["input"])


def reference(content):
    return {"authority": "wharf", "digest": hashlib.sha256(content).hexdigest()}


def record(value):
    return {"schema": 2, "entry": {"kind": "operation", "value": value}}


class OperationContract(unittest.TestCase):
    def test_actual_rust_byte_digest_and_refusal_observations(self):
        self.assertEqual(len(CASES), 223)
        self.assertEqual(sum(not case["refused"] for case in CASES.values()), 85)
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["id"]):
                if case["refused"]:
                    self.assertRaises(Refusal, codec.load, case["kind"], case["input"])
                else:
                    value = codec.load(case["kind"], case["input"])
                    self.assertEqual(codec.encode(case["kind"], value), case["encoded"].encode())
                    self.assertEqual(codec.digest(case["kind"], value), case["digest"])

    def test_actual_rust_admission_matrix(self):
        self.assertEqual(len(FIXTURE["admissions"]), 200)
        for case in FIXTURE["admissions"]:
            with self.subTest(case=case["id"]):
                declared = None if case["declaration"] is None else json.loads(case["declaration"])
                self.assertEqual(codec.admit(json.loads(case["request"]), json.loads(case["assessment"]), declared), case["reason"])

    def test_actual_rust_verified_settlement_observations(self):
        self.assertEqual(len(FIXTURE["settlements"]), 22)
        self.assertEqual(sum(not case["refused"] for case in FIXTURE["settlements"]), 7)
        for case in FIXTURE["settlements"]:
            with self.subTest(case=case["id"]):
                args = (json.loads(case["request"]), json.loads(case["expected"]), json.loads(case["observed"]), case["content"].encode())
                if case["refused"]:
                    self.assertRaises(Refusal, codec.settle, *args)
                else:
                    operation = codec.settle(*args)
                    self.assertEqual(codec.encode("operation", operation), case["encoded"].encode())

    def test_every_action_has_its_own_explicit_precondition(self):
        for action in ("inspect", "build", "publish", "install", "uninstall", "deploy", "dispose"):
            value = example("request-" + action)
            with self.subTest(action=action):
                del value["expected"]
                if action in ("inspect", "build"):
                    self.assertIsNone(codec.read("request", value)["expected"])
                else:
                    self.assertRaises(Refusal, codec.read, "request", value)

    def test_publish_never_overwrites_a_present_state(self):
        value = example("request-publish")
        value["expected"] = example("state-present")
        self.assertRaises(Refusal, codec.read, "request", value)

    def test_unknown_state_never_supplies_a_mutation_precondition(self):
        for action in ("publish", "install", "uninstall", "deploy", "dispose"):
            value = example("request-" + action)
            value["expected"] = {"state": "unknown"}
            self.assertRaises(Refusal, codec.read, "request", value)

    def test_context_and_state_revisions_do_not_use_python_numeric_equality(self):
        for revision in (True, 1.0, -1, 18446744073709551616):
            value = example("state-present")
            value["revision"] = revision
            self.assertRaises(Refusal, codec.read, "state", value)
        value = example("state-zero")
        self.assertEqual(codec.read("state", value)["revision"], 0)
        conditions = example("conditions")
        conditions["observed"] = value
        self.assertRaises(Refusal, codec.read, "conditions", conditions)

    def test_assessment_cannot_be_reused_after_request_change(self):
        value = example("operation-deploy")
        value["request"]["context"]["request"] = "0" * 64
        self.assertEqual(codec.admit(value["request"], value["assessment"]), "conflict")
        self.assertRaises(Refusal, codec.read, "operation", value)

    def test_mutations_require_current_idle_writer(self):
        for writer, reason in (("active", "conflict"), ("unknown", "unavailable")):
            value = example("operation-deploy")
            value["assessment"]["conditions"]["writer"] = writer
            self.assertEqual(codec.admit(value["request"], value["assessment"]), reason)
            self.assertRaises(Refusal, codec.read, "operation", value)

    def test_non_mutating_actions_do_not_require_writer_quiescence(self):
        for action in ("inspect", "build"):
            value = example("operation-" + action)
            value["assessment"]["conditions"]["writer"] = "active"
            value["outcome"]["writer"] = "unknown"
            self.assertIsNone(codec.admit(value["request"], value["assessment"]))
            self.assertEqual(codec.read("operation", value)["outcome"]["writer"], "unknown")

    def test_applied_mutations_must_finish_with_idle_writer(self):
        for writer in ("active", "unknown"):
            value = example("operation-deploy")
            value["outcome"]["writer"] = writer
            self.assertRaises(Refusal, codec.read, "operation", value)

    def test_unknown_observation_can_admit_inspection_but_not_prove_material(self):
        value = example("operation-inspect")
        value["assessment"]["conditions"]["observed"] = {"state": "unknown"}
        self.assertIsNone(codec.admit(value["request"], value["assessment"]))
        self.assertRaises(Refusal, codec.read, "operation", value)
        value["outcome"] = {"outcome": "unknown", "reason": "unverified"}
        self.assertEqual(codec.read("operation", value)["outcome"]["outcome"], "unknown")

    def test_state_comparison_uses_both_revision_and_artifact_identity(self):
        for field, changed in (("revision", 4), ("digest", "0" * 64)):
            value = example("operation-dispose")
            value["assessment"]["conditions"]["observed"][field] = changed
            self.assertEqual(codec.admit(value["request"], value["assessment"]), "conflict")
            self.assertRaises(Refusal, codec.read, "operation", value)

    def test_refusal_priority_preserves_unknown_and_unmet_distinctions(self):
        value = example("operation-deploy")
        value["assessment"]["conditions"]["authorization"] = {"state": "unknown"}
        value["assessment"]["conditions"]["writer"] = "active"
        self.assertEqual(codec.admit(value["request"], value["assessment"]), "unverified")
        value["outcome"] = {"outcome": "refused", "reason": "conflict"}
        self.assertRaises(Refusal, codec.read, "operation", value)
        value["outcome"] = {"outcome": "unknown", "reason": "unverified"}
        self.assertEqual(codec.read("operation", value)["outcome"], value["outcome"])
        value["assessment"]["capabilities"] = []
        self.assertEqual(codec.admit(value["request"], value["assessment"]), "unsupported")
        self.assertRaises(Refusal, codec.read, "operation", value)

    def test_provider_can_refuse_or_be_unknown_after_admission(self):
        for outcome, reason in (("refused", "denied"), ("unknown", "unavailable")):
            value = example("operation-deploy")
            self.assertIsNone(codec.admit(value["request"], value["assessment"]))
            value["outcome"] = {"outcome": outcome, "reason": reason}
            self.assertEqual(codec.read("operation", value)["outcome"], value["outcome"])

    def test_exact_material_is_more_than_a_content_digest(self):
        for field, changed in (("source", "0" * 40), ("build", None)):
            value = example("operation-deploy")
            artifact = value["outcome"]["material"]["artifact"]
            if field == "source":
                artifact[field]["tree"] = changed
            else:
                artifact[field] = changed
            self.assertRaises(Refusal, codec.read, "operation", value)

    def test_build_binds_source_and_build_but_discovers_content_digest(self):
        value = example("operation-build")
        value["outcome"]["material"]["artifact"]["digest"] = "9" * 64
        self.assertEqual(codec.read("operation", value)["outcome"]["material"]["artifact"]["digest"], "9" * 64)

    def test_delete_material_cannot_be_present(self):
        for action in ("uninstall", "dispose"):
            value = example("operation-" + action)
            value["outcome"]["material"] = example("material-present")
            self.assertRaises(Refusal, codec.read, "operation", value)

    def test_unknown_record_readback_is_not_completion(self):
        value = example("operation-deploy")
        value["outcome"] = {"outcome": "unknown", "reason": "unverified"}
        content = codec.encode("record", record(value))
        ref = reference(content)
        self.assertEqual(codec.verify(ref, ref, content)["entry"]["value"]["outcome"]["outcome"], "unknown")
        self.assertRaises(Refusal, codec.settle, value["request"], ref, ref, content)

    def test_encoder_revalidates_and_readback_returns_detached_observations(self):
        value = example("operation-deploy")
        original = copy.deepcopy(value)
        content = codec.encode("record", record(value))
        ref = reference(content)
        result = codec.settle(value["request"], ref, ref, content)
        result["request"]["context"]["requested"]["app"] = "other"
        self.assertEqual(value, original)
        value["outcome"]["writer"] = "unknown"
        self.assertRaises(Refusal, codec.encode, "operation", value)
        self.assertRaises(Refusal, codec.digest, "operation", value)

    def test_declaration_binding_checks_roles_before_execution(self):
        value = example("operation-deploy")
        declared = json.loads(next(case["declaration"] for case in FIXTURE["admissions"] if case["id"] == "declared"))
        self.assertIsNone(codec.admit(value["request"], value["assessment"], declared))
        value["assessment"]["conditions"]["authorization"]["state"] = "denied"
        value["assessment"]["conditions"]["authorization"]["evidence"]["authority"] = "other"
        self.assertEqual(codec.admit(value["request"], value["assessment"], declared), "unverified")
        value["assessment"]["conditions"]["authorization"]["evidence"]["authority"] = "ensign"
        self.assertEqual(codec.admit(value["request"], value["assessment"], declared), "denied")

    def test_private_fields_and_raw_receipts_are_not_public_contracts(self):
        for kind in ("intent", "request", "assessment", "operation"):
            name = {"intent": "intent-deploy", "request": "request-deploy", "assessment": "assessment", "operation": "operation-deploy"}[kind]
            value = example(name)
            value["token"] = "private"
            self.assertRaises(Refusal, codec.read, kind, value)
        self.assertRaises(Refusal, codec.read, "receipt", {})
