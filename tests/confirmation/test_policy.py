import copy
import json
import unittest
from unittest import mock

from lib.process.confirmation import admission, history, policy, store
from lib.refusal import Refusal
from lib.store.lane import documents, public
from tests.confirmation.admitted import confirmed
from tests.workflow import fixtures


class Policy(unittest.TestCase):
    def test_caller_selects_only_bounded_exact_original_scope(self):
        selector, pins, readers, trusted, _ = confirmed()
        self.assertEqual(policy.requested(json.dumps(selector)), selector)
        for value in (None, b"{}", "{}", " " * 65537, "\ud800", '{"a":1,"a":2}', "[]", "NaN", "Infinity"):
            with self.subTest(value=str(value)[:30]), self.assertRaises(Refusal):
                policy.requested(value)
        for key, value in (("caller", "carol"), ("workflow", "main"), ("idle", True), ("current", pins["current"]),
                           ("source", pins["dispatch"]["source"]), ("endpoint", "https://foreign.example.test"),
                           ("schema", "wharf.lane.dispatch/v1"), ("request", "0" * 64),
                           ("original", dict(selector["original"], digest="0" * 64)),
                           ("target", dict(selector["target"], lane="foreign"))):
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.admit(dict(selector, **{key: value}), pins, readers, trusted)
        self.assertFalse(readers[0].calls)
        self.assertFalse(trusted[1].calls)
        self.assertFalse(trusted[2].calls)

    def test_trusted_pins_are_strict_bounded_and_qualified_before_io(self):
        faults = [([], "extra", True), ([], "schema", "v2"), (["dispatch"], "caller", "carol"),
                  (["dispatch"], "workflow", "PerishLab/wharf/.github/workflows/ship.yml@" + history.COMMIT),
                  (["context"], "commit", "1" * 40), (["context"], "run", True),
                  (["actors", "actor"], "id", 20.0), (["actors", "triggering_actor"], "extra", True),
                  (["observer"], "extra", True), (["observer", "context"], "run", 123),
                  (["observer", "context"], "attempt", 2), (["observer", "context"], "attempt", True),
                  (["observer", "context"], "run", 2 ** 64), (["observer", "actors", "actor"], "id", 20),
                  (["observer", "actors", "triggering_actor"], "login", "../dave"),
                  (["current"], "digest", "bad"), (["current"], "etag", "\n"), (["current"], "etag", "x" * 257),
                  (["publication"], "origin", "https://evidence.example.test/path"),
                  (["publication"], "authority", "foreign"), (["publication"], "extra", True)]
        for path, key, value in faults:
            selector, pins, readers, trusted, _ = confirmed()
            section = pins
            for part in path:
                section = section[part]
            section[key] = value
            with self.subTest(path=path, key=key), self.assertRaises(Refusal):
                admission.admit(selector, pins, readers, trusted)
            self.assertFalse(readers[0].calls)
            self.assertFalse(trusted[1].calls)
        selector, pins, readers, trusted, _ = confirmed()
        for key in pins:
            changed = copy.deepcopy(pins)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(Refusal):
                admission.admit(selector, changed, readers, trusted)
        pins["padding"] = "x" * 65537
        self.assertRaises(Refusal, admission.admit, selector, pins, readers, trusted)

    def test_custody_boolean_float_and_extra_field_aliases_refuse(self):
        for key, value in (("attempt", True), ("attempt", 1.0), ("run", 123.0), ("extra", True)):
            selector, pins, readers, trusted, body = confirmed()
            key_name = readers[0].keys["custody"]
            custody = documents.decode(readers[0].bucket.objects[key_name])
            readers[0].bucket.objects[key_name] = documents.encode(dict(custody, **{key: value}))
            with self.subTest(key=key, value=value), mock.patch.object(public.Reader, "read", return_value=body) as read, self.assertRaises(Refusal):
                admission.admit(selector, pins, readers, trusted)
            self.assertFalse(trusted[1].calls)
            read.assert_not_called()

    def test_environment_boundary_rejects_writers_and_old_or_retried_platform_before_clients(self):
        selector, pins, _, trusted, _ = confirmed()
        environ = dict(fixtures.platform(), GITHUB_RUN_ID="456", GITHUB_SHA="2" * 40, GITHUB_WORKFLOW_SHA="2" * 40)
        for key in admission.WRITERS + tuple(prefix + "ACCESS_KEY_ID" for prefix in admission.PREFIXES):
            with self.subTest(writer=key), mock.patch.object(store, "Reader") as reader, self.assertRaises(Refusal):
                admission.observe(json.dumps(selector), pins, dict(environ, **{key: "held"}))
            reader.assert_not_called()
        for change in ({"GITHUB_RUN_ID": "123"}, {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_SHA": history.COMMIT},
                       {"GITHUB_WORKFLOW_SHA": history.COMMIT}, {"GITHUB_EVENT_NAME": "repository_dispatch"},
                       {"GITHUB_REF": "refs/heads/topic"}, {"GITHUB_WORKFLOW_REF": "foreign"}):
            with self.subTest(change=change), mock.patch.object(store, "Reader") as reader, self.assertRaises(Refusal):
                admission.observe(json.dumps(selector), pins, dict(environ, **change))
            reader.assert_not_called()
        self.assertEqual(trusted[0], pins["observer"]["context"])

    def test_platform_context_alias_refuses_before_dependent_reads(self):
        selector, pins, readers, trusted, _ = confirmed()
        for context in (dict(trusted[0], attempt=True), dict(trusted[0], run=456.0), dict(trusted[0], extra=True)):
            with self.subTest(context=context), self.assertRaises(Refusal):
                admission.admit(selector, pins, readers, (context, *trusted[1:]))
        self.assertFalse(readers[0].calls)
        self.assertFalse(trusted[1].calls)

    def test_environment_entry_uses_separate_prepared_read_clients_and_fixed_platform(self):
        selector, pins, readers, trusted, body = confirmed()
        environ = dict(fixtures.platform(), GITHUB_RUN_ID="456", GITHUB_SHA="2" * 40, GITHUB_WORKFLOW_SHA="2" * 40,
                       WHARF_PREVIEW_CONTROL_TOKEN="control-reader", WHARF_PREVIEW_PRODUCT_TOKEN="product-reader")
        with mock.patch.object(store, "Reader", return_value=readers[0]) as reader, mock.patch.object(admission, "GitHub", side_effect=trusted[1:]) as github, mock.patch.object(public.Reader, "read", return_value=body):
            result = admission.observe(json.dumps(selector), pins, environ)
        self.assertEqual(result["authority"]["workflow"]["run"], 456)
        reader.assert_called_once_with(pins["dispatch"], environ)
        self.assertEqual(github.call_args_list, [mock.call("control-reader"), mock.call("product-reader")])


class Transport(unittest.TestCase):
    def test_exact_four_key_reader_has_only_bounded_signed_gets_and_no_writer(self):
        selector, pins, _, _, _ = confirmed()
        environ = {"WHARF_LANE_READER_" + key: value for key, value in {
                   "ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "BUCKET": "lane-state",
                   "ACCESS_KEY_ID": "reader", "SECRET_ACCESS_KEY": "private-reader"}.items()}
        reader = store.Reader(pins["dispatch"], environ)
        self.assertFalse(any(hasattr(reader, key) for key in ("get", "create", "swap", "delete", "list")))
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda key: '"version"' if key == "ETag" else None
        response.read.return_value = b"{}"
        reader.connect = mock.Mock(return_value=connection)
        for key in store.keys(pins["dispatch"]).values():
            self.assertEqual(reader.snapshot(key), (b"{}", '"version"'))
            self.assertEqual(connection.request.call_args.args, ("GET", "/lane-state/" + key))
            self.assertEqual(response.read.call_args.args, (65537,))
            self.assertIn("authorization", connection.request.call_args.kwargs["headers"])
        calls = reader.connect.call_count
        root = documents.location(selector["target"])
        for key in (root + "/requests/" + "0" * 64 + "/intent.json", root + "/unknown.json", root + "/current.json/../intent.json"):
            with self.subTest(key=key), self.assertRaises(Refusal):
                reader.snapshot(key)
        self.assertEqual(reader.connect.call_count, calls)
        for status in (301, 403, 429, 500):
            response.status = status
            self.assertRaises(Refusal, reader.snapshot, root + "/current.json")
        response.status = 200
        response.read.return_value = b"x" * 65537
        self.assertRaises(Refusal, reader.snapshot, root + "/current.json")


if __name__ == "__main__":
    unittest.main()
