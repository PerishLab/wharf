import copy
import hashlib
import json
import unittest

from lib.content import canonical, resources
from lib.content.lane import codec
from lib.refusal import Refusal

FIXTURE = resources.read_json("identity/lane-fixtures.json")
CASES = {case["id"]: case for case in FIXTURE["cases"]}


def example(kind):
    return json.loads(CASES[kind]["input"])


def reference(content):
    return {"authority": "wharf", "digest": hashlib.sha256(content).hexdigest()}


class LaneCodec(unittest.TestCase):
    def test_actual_rust_byte_and_digest_observations(self):
        self.assertEqual(len(CASES), 157)
        self.assertEqual(sum(not case["refused"] for case in CASES.values()), 43)
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["id"]):
                if case["refused"]:
                    self.assertRaises(Refusal, codec.load, case["kind"], case["input"])
                else:
                    value = codec.load(case["kind"], case["input"])
                    self.assertEqual(codec.encode(case["kind"], value), case["encoded"].encode())
                    self.assertEqual(codec.digest(case["kind"], value), case["digest"])
                    self.assertEqual(codec.load(case["kind"], case["encoded"].encode()), value)

    def test_protocol_uses_typed_order_not_general_canonical_order(self):
        for kind in ("address", "source", "artifact", "declaration", "context", "selection", "record"):
            with self.subTest(kind=kind):
                value = example(kind)
                self.assertNotEqual(codec.encode(kind, value), canonical.encode(value))

    def test_capabilities_use_enum_order(self):
        value = codec.read("capabilities", example("capabilities"))
        self.assertEqual(value, ["inspect", "build", "publish", "install", "uninstall", "deploy", "dispose"])
        self.assertNotEqual(value, sorted(value))

    def test_artifact_digest_is_not_encoded_identity(self):
        artifact = example("artifact")
        self.assertNotEqual(artifact["digest"], codec.digest("artifact", artifact))

    def test_build_is_optional_but_not_an_empty_object(self):
        artifact = example("artifact")
        del artifact["build"]
        self.assertIsNone(codec.read("artifact", artifact)["build"])
        artifact["build"] = {}
        self.assertRaises(Refusal, codec.read, "artifact", artifact)

    def test_generic_families_and_adaptors_do_not_require_static_actions(self):
        declaration = example("declaration")
        declaration.update(adaptor="binary", capabilities=["install", "publish", "uninstall"])
        declaration["target"]["lane"] = "binary.review"
        value = codec.read("declaration", declaration)
        self.assertEqual(value["capabilities"], ["publish", "install", "uninstall"])
        declaration["capabilities"] = []
        self.assertEqual(codec.read("declaration", declaration)["capabilities"], [])

    def test_readers_produce_detached_values(self):
        value = example("record")
        original = copy.deepcopy(value)
        normalized = codec.read("record", value)
        normalized["entry"]["value"]["observation"]["artifact"]["source"]["repository"] = "Other/repo"
        self.assertEqual(value, original)

    def test_encoder_revalidates_a_mutated_value(self):
        value = codec.read("selection", example("selection"))
        value["observation"]["actual"]["lane"] = "preview.other"
        self.assertRaises(Refusal, codec.encode, "selection", value)
        self.assertRaises(Refusal, codec.digest, "selection", value)

    def test_reference_verifies_exact_supplied_selection_bytes(self):
        content = codec.encode("record", example("record"))
        expected = reference(content)
        self.assertEqual(codec.verify(expected, copy.deepcopy(expected), content), example("record"))

    def test_reference_refuses_other_authority_digest_or_content(self):
        content = codec.encode("record", example("record"))
        expected = reference(content)
        for observed in ({**expected, "authority": "ensign"}, {**expected, "digest": "0" * 64}):
            with self.subTest(observed=observed):
                self.assertRaises(Refusal, codec.verify, expected, observed, content)
        self.assertRaises(Refusal, codec.verify, expected, expected, content + b" ")
        self.assertRaises(Refusal, codec.verify, expected, expected, content.decode())

    def test_matching_digest_does_not_admit_noncanonical_readback(self):
        value = example("record")
        artifact = value["entry"]["value"]["observation"]["artifact"]
        for content in (json.dumps(value).encode(), canonical.encode(value), codec.encode("record", value) + b"\n"):
            with self.subTest(content=content):
                self.assertRaises(Refusal, codec.verify, reference(content), reference(content), content)
        del artifact["build"]
        content = json.dumps(value, separators=(",", ":")).encode()
        self.assertRaises(Refusal, codec.verify, reference(content), reference(content), content)

    def test_digest_does_not_bypass_selection_validation(self):
        value = example("record")
        value["entry"]["value"]["observation"]["actual"]["app"] = "other"
        content = json.dumps(value, separators=(",", ":")).encode()
        self.assertRaises(Refusal, codec.verify, reference(content), reference(content), content)

    def test_operation_records_are_explicitly_outside_this_batch(self):
        value = example("record")
        value["entry"]["kind"] = "operation"
        self.assertRaises(Refusal, codec.read, "record", value)
        self.assertRaises(Refusal, codec.read, "operation", {})

    def test_non_object_structs_and_malformed_json_refuse(self):
        for kind in ("address", "artifact", "build", "declaration", "context", "selection", "record", "reference"):
            with self.subTest(kind=kind):
                self.assertRaises(Refusal, codec.read, kind, [])
        for content in (b"\xff", b"{}", b"{} trailing", b"", b"\xef\xbb\xbf{}", True, {}):
            with self.subTest(content=content):
                self.assertRaises(Refusal, codec.load, "record", content)

    def test_non_integer_revision_and_schema_cannot_use_python_equality(self):
        for value in (True, 1.0, 0, -1, 18446744073709551616):
            context = example("context")
            context["revision"] = value
            self.assertRaises(Refusal, codec.read, "context", context)
        for value in (True, 2.0):
            record = example("record")
            record["schema"] = value
            self.assertRaises(Refusal, codec.read, "record", record)

    def test_private_fields_are_not_a_generic_artifact_contract(self):
        for field in ("stamp", "url", "token", "delete"):
            value = example("artifact")
            value[field] = "private"
            self.assertRaises(Refusal, codec.read, "artifact", value)

    def test_each_uncertain_reason_stays_unknown(self):
        for reason in ("unavailable", "unverified"):
            value = example("selection")
            value["observation"] = {"outcome": "unknown", "reason": reason}
            self.assertEqual(codec.read("selection", value)["observation"]["outcome"], "unknown")
            value["observation"]["outcome"] = "unmet"
            self.assertRaises(Refusal, codec.read, "selection", value)

    def test_duplicate_nested_fields_cannot_disappear(self):
        value = example("record")
        content = json.dumps(value).replace('"actual": {', '"actual": {"app": "other",')
        self.assertRaises(Refusal, codec.load, "record", content)
