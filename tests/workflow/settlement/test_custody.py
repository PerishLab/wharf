import copy
import unittest

from lib.content import canonical
from lib.process.lane import execution, settlement
from lib.refusal import Refusal
from lib.store.lane import documents
from tests.workflow.settlement import fixtures


class Custody(unittest.TestCase):
    def test_workflow_json_order_does_not_change_complete_custody_readback(self):
        for action in ("apply", "dispose"):
            held = fixtures.bundle(action)
            key = execution.location(held["intent"])
            original = held["store"].objects[key]
            transported = canonical.encode(held["value"])
            custody = documents.decode(transported)["custody"]
            self.assertEqual(documents.decode(original), custody)
            self.assertNotEqual(original, documents.encode(custody))
            held["held"]["prepared"] = transported.decode()
            with self.subTest(action=action), fixtures.runtime(held):
                result = settlement.perform(held["held"], held["env"])
                self.assertEqual(result["phase"], "settled")
            self.assertIsNone(held["store"].current["active"])
            self.assertEqual(held["store"].objects[key], original)

    def test_final_custody_readback_refuses_changed_missing_duplicate_or_foreign_values(self):
        sample = fixtures.bundle()["value"]["custody"]
        invalid = [None, b"{", b"[]", b"x" * 65537, b'{"schema":1,"schema":1}']
        invalid.extend(documents.encode(dict(sample, **{key: None})) for key in sample)
        invalid.extend(documents.encode({name: value for name, value in sample.items() if name != key}) for key in sample)
        invalid.append(documents.encode(dict(sample, foreign=True)))
        invalid.append(documents.encode(dict(sample, attempt=True)))
        invalid.append(documents.encode(dict(sample, attempt=float(sample["attempt"]))))
        for body in invalid:
            held = fixtures.bundle()
            key = execution.location(held["intent"])
            original = held["store"].swap

            def swap(location, value, etag):
                original(location, value, etag)
                held["store"].objects[key] = copy.deepcopy(body)

            held["store"].swap = swap
            with self.subTest(body=body), fixtures.runtime(held), self.assertRaises(Refusal):
                settlement.perform(held["held"], held["env"])
            self.assertEqual(held["store"].current["phase"], "settled")
            self.assertEqual(sum(call[0] == "swap" for call in held["store"].calls), 1)


if __name__ == "__main__":
    unittest.main()
