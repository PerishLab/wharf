import copy
import hashlib
import json
import unittest
from unittest import mock

from lib.content import resources
from lib.content.lane import codec
from lib.refusal import Refusal
from lib.store.lane import public, publication, state
from tests.lib.identity.test_publication import declared
from tests.lib.store.memory import Memory

FIXTURE = resources.read_json("identity/lane-publication-fixtures.json")["confirmation"]
CASES = {case["id"]: case for case in FIXTURE["cases"]}
LINKS = {case["id"]: case for case in FIXTURE["links"]}


def provided(value):
    reference = value["reference"]
    return codec.evidence(reference, reference, value["content"].encode())


def linked(case):
    return codec.confirm(case["request"], provided(case["current"]), provided(case["original"]))


class ConfirmationCodec(unittest.TestCase):
    def test_actual_rust_schema_canonical_bytes_and_digest_observations(self):
        self.assertEqual(FIXTURE["provenance"]["source"], "5eb1dbdbfe00960eb607b752079225e773193779")
        self.assertEqual(len(CASES), 21)
        self.assertEqual(sum(not case["refused"] for case in CASES.values()), 3)
        for case in CASES.values():
            with self.subTest(case=case["id"]):
                if case["refused"]:
                    self.assertRaises(Refusal, codec.load, case["kind"], case["input"])
                else:
                    value = codec.load(case["kind"], case["input"])
                    self.assertEqual(codec.encode(case["kind"], value), case["encoded"].encode())
                    self.assertEqual(codec.digest(case["kind"], value), case["digest"])
        self.assertEqual(CASES["original"]["digest"], "74a9ca8335473f0f664d0a59200613a5f7e29e17ba24b5310b42c6def4fd8034")
        self.assertEqual(CASES["confirmation"]["digest"], "d3dbdf2186325bbf291765669fe645a45eb20f5e48eb0a1fdae93a0b4670f51c")

    def test_actual_rust_exact_linked_original_unknown_observations(self):
        self.assertEqual(len(LINKS), 11)
        self.assertEqual(sum(not case["refused"] for case in LINKS.values()), 2)
        for case in LINKS.values():
            with self.subTest(case=case["id"]):
                if case["refused"]:
                    self.assertRaises(Refusal, linked, case)
                else:
                    self.assertEqual(codec.encode("confirmation", linked(case)), case["encoded"].encode())

    def test_supplied_readback_and_returned_confirmation_are_detached(self):
        case = LINKS["exact"]
        observed, original = provided(case["current"]), provided(case["original"])
        saved = copy.deepcopy((observed, original))
        result = codec.confirm(case["request"], observed, original)
        result["authorization"]["digest"] = "0" * 64
        result["operation"]["assessment"]["conditions"]["writer"] = "active"
        self.assertEqual((observed, original), saved)
        observed["record"]["entry"]["value"]["operation"]["request"]["context"]["revision"] = True
        self.assertRaises(Refusal, codec.confirm, case["request"], observed, original)

    def test_counterfeit_missing_moved_and_oversized_supplied_readbacks_refuse(self):
        case = LINKS["exact"]
        observed, original = provided(case["current"]), provided(case["original"])
        for value in ({}, {"reference": observed["reference"]}, dict(observed, extra=True), None):
            with self.subTest(value=value):
                self.assertRaises(Refusal, codec.confirm, case["request"], value, original)
        changed = copy.deepcopy(observed)
        changed["reference"]["digest"] = "0" * 64
        self.assertRaises(Refusal, codec.confirm, case["request"], changed, original)
        reference = case["current"]["reference"]
        moved = dict(reference, digest="0" * 64)
        self.assertRaises(Refusal, codec.evidence, reference, moved, case["current"]["content"].encode())
        for content in (case["current"]["content"], b" " * 65537, None):
            self.assertRaises(Refusal, codec.evidence, reference, reference, content)

    def test_ordinary_settlement_publication_and_state_never_consume_confirmation(self):
        case = LINKS["exact"]
        reference = case["current"]["reference"]
        content = case["current"]["content"].encode()
        record = codec.load("record", content)
        self.assertRaises(Refusal, codec.settle, case["request"], reference, reference, content)
        reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
        writer = Memory()
        with self.assertRaisesRegex(Refusal, "separate authorization consumer"):
            publication.publish(writer, reader, declared(), record)
        self.assertEqual(writer.writes, [])
        with mock.patch.object(public.Reader, "read", return_value=content), self.assertRaisesRegex(Refusal, "separate authorization consumer"):
            publication.settle(reader, declared(), reference, case["request"])
        bucket = mock.Mock(spec=("snapshot", "get", "create", "swap"))
        self.assertRaises(Refusal, state.record, bucket, record)
        self.assertEqual(bucket.mock_calls, [])

    def test_nonmutating_valid_operations_cannot_be_confirmed(self):
        value = json.loads(CASES["confirmation"]["input"])["entry"]["value"]
        for action in ("inspect", "build"):
            changed = copy.deepcopy(value)
            operation = changed["operation"]
            request = operation["request"]
            request["expected"] = None
            request["intent"] = {"action": action}
            if action == "build":
                artifact = operation["outcome"]["material"]["artifact"]
                artifact["build"] = {"inputs": "1" * 64, "world": "2" * 64}
                request["intent"].update(source=artifact["source"], build=artifact["build"])
            else:
                operation["outcome"]["material"] = {"state": "absent"}
            operation["assessment"]["request"] = codec.digest("request", request)
            operation["assessment"]["capabilities"] = [action]
            codec.read("operation", operation)
            self.assertRaises(Refusal, codec.read, "confirmation", changed)

    def test_wrong_exact_request_cannot_be_hidden_by_a_new_matching_assessment(self):
        case = LINKS["exact"]
        observed, original = provided(case["current"]), provided(case["original"])
        value = observed["record"]["entry"]["value"]
        operation = value["operation"]
        operation["request"]["context"]["revision"] = 3
        operation["assessment"]["request"] = codec.digest("request", operation["request"])
        observed["reference"]["digest"] = codec.digest("record", observed["record"])
        self.assertRaises(Refusal, codec.confirm, operation["request"], observed, original)
        self.assertEqual(hashlib.sha256(case["current"]["content"].encode()).hexdigest(), case["current"]["reference"]["digest"])


if __name__ == "__main__":
    unittest.main()
