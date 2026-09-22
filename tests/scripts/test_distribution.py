import json
import unittest

from unittest import mock

from lib import parameters
from scripts import distribution

HELD = {"repository": "PerishLab/plumb", "marker": "v0.42.0", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1", "needs": json.dumps({"plan": {"result": "success"}})}


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in distribution.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())


class Record(unittest.TestCase):
    def test_the_record_is_written_to_the_product_release_bucket_and_read_back_from_its_authority(self):
        seen = {}

        def record(bucket, context, needs, reader):
            seen.update(bucket=bucket, context=context, needs=needs)
            with mock.patch.object(distribution.release, "fetch", side_effect=lambda url: url):
                seen["read"] = reader("v1/releases/stable/v0.42.0/distribution.json")
            return {"state": "complete"}

        with mock.patch.object(distribution.r2, "writer", side_effect=lambda name, role: (name, role)), mock.patch.object(distribution.distribution, "record", side_effect=record):
            self.assertEqual(distribution.record(HELD), {"state": "complete"})
        self.assertEqual(seen["bucket"], ("perish-plumb-releases", "RELEASES"))
        self.assertEqual(seen["read"], "https://releases.plumb.perish.uk/v1/releases/stable/v0.42.0/distribution.json")
        self.assertEqual((seen["context"]["commit"], seen["needs"]), ("a" * 40, {"plan": {"result": "success"}}))
