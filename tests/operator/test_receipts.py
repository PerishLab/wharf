import json
import unittest

from lib.refusal import Refusal
from tests.cfworker import test_deployment
from tests.lib.process.test_cfworker import Response


class Receipts(unittest.TestCase):
    def setUp(self):
        self.fixture = test_deployment.Deployment()
        self.fixture.setUp()

    def test_absent_redundant_fields_accept_but_explicit_conflicts_refuse(self):
        fixture = self.fixture
        for omitted in (("preview_id",), ("preview_name",), ("preview_id", "preview_name")):
            api = fixture.client()
            value = {key: item for key, item in fixture.raw().items() if key not in omitted}
            fixture.response(api, value)
            self.assertEqual(fixture.invoke(api)["deployment"], fixture.projected())
        for field in ("preview_id", "preview_name"):
            api = fixture.client()
            fixture.response(api, {"id": fixture.raw()["id"], field: "foreign"})
            self.assertFalse(fixture.invoke(api)["acknowledged"])

    def test_missing_errors_accepts_but_malformed_or_nonempty_errors_refuse(self):
        fixture = self.fixture
        for errors in ("missing", None, [], {}, "", False, ["failure"]):
            api = fixture.client()
            envelope = {"success": True, "result": fixture.raw()}
            if errors != "missing":
                envelope["errors"] = errors
            body = json.dumps(envelope).encode()
            api._client.open.side_effect = lambda request, timeout: Response(body, request.full_url)
            self.assertEqual(fixture.invoke(api)["acknowledged"], errors in ("missing", None, []))
            with self.assertRaises(Refusal):
                fixture.invoke(api)
