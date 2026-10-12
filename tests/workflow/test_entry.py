import copy
import unittest
from unittest import mock

from lib.content import canonical
from lib.process.lane import entry
from lib.refusal import Refusal
from lib.store.lane import registration, state
from tests.lib.preview.test_admission import CONTEXT
from tests.workflow import fixtures


class Admission(unittest.TestCase):
    def test_default_and_keyed_bindings_use_shared_namespace(self):
        for lane in ("preview", "preview.design", "preview.review"):
            target = fixtures.registered(lane=lane)
            requested = fixtures.request(target=target)
            store = fixtures.Store(target)
            observed = entry.admit(requested, store, CONTEXT, fixtures.provider())
            self.assertEqual(observed["intent"], canonical.digest(requested))
            self.assertEqual(observed["registration"], target)
            self.assertEqual(observed["current"], state.initial(target))
            self.assertEqual(store.calls, [("snapshot", registration.location(requested["target"])),
                                           ("snapshot", registration.location(requested["target"]).replace("registration.json", "current.json"))])

    def test_inspect_and_dispose_never_observe_product_source(self):
        for operation in ("inspect", "dispose"):
            api = fixtures.provider()
            target = fixtures.registered()
            current = fixtures.settled(target) if operation == "dispose" else None
            result = entry.admit(fixtures.request(operation), fixtures.Store(target, current), CONTEXT, api)
            self.assertFalse(any(call[0] == "commit" for call in api.calls))
            self.assertEqual(result["repository"]["name"], "PerishLab/crest")

    def test_legacy_release_foreign_and_caller_supplied_controls_refuse(self):
        base = fixtures.request()
        values = [dict(base, schema="wharf.preview.request/v1"), dict(base, operation="publish"),
                  dict(base, token="not-echoed"), dict(base, expected={"state": "unknown"}),
                  dict(base, target=dict(base["target"], repository="foreign/crest")),
                  dict(base, target=dict(base["target"], lane="preview.a.b")), dict(base, revision=True)]
        for value in values:
            with self.subTest(value=value), self.assertRaises(Refusal) as observed:
                entry.requested(fixtures.encoded(value))
            self.assertNotIn("not-echoed", str(observed.exception))
        for body in (None, "\ud800", "x" * 65537, '{"schema":"x","schema":"y"}'):
            with self.subTest(body=repr(body)[:30]), self.assertRaises(Refusal):
                entry.requested(body)

    def test_disabled_changed_and_busy_registration_stop_before_github(self):
        target = fixtures.registered()
        variants = [dict(target, enabled=False), dict(target, generation="0" * 64)]
        for changed in variants:
            api = fixtures.provider()
            with self.assertRaises(Refusal):
                entry.admit(fixtures.request(), fixtures.Store(changed), CONTEXT, api)
            self.assertEqual(api.calls, [])
        requested = fixtures.request()
        requested["revision"] = 2
        api = fixtures.provider()
        with self.assertRaises(Refusal):
            entry.admit(requested, fixtures.Store(target), CONTEXT, api)
        self.assertEqual(api.calls, [])

    def test_platform_is_fixed_lane_main_not_ship_or_preview(self):
        self.assertEqual(entry.platform(fixtures.platform()), CONTEXT)
        for change in ({"GITHUB_WORKFLOW_REF": "PerishLab/wharf/.github/workflows/ship.yml@refs/heads/main"},
                       {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_WORKFLOW_SHA": "2" * 40}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                entry.platform(dict(fixtures.platform(), **change))

    def test_both_actors_exact_source_and_current_run_are_independent(self):
        cases = [("caller", "bob"), ("workflow", "caller-authored")]
        for key, changed in cases:
            with self.subTest(key=key), self.assertRaises(Refusal):
                entry.admit(dict(fixtures.request(), **{key: changed}), fixtures.Store(fixtures.registered()), CONTEXT, fixtures.provider())
        for change in ({"status": "completed"}, {"path": ".github/workflows/ship.yml"}, {"run_attempt": 2}):
            api = fixtures.provider()
            api.observed.update(change)
            with self.assertRaises(Refusal):
                entry.admit(fixtures.request(), fixtures.Store(fixtures.registered()), CONTEXT, api)
        api = fixtures.provider()
        api.permissions["bob"]["permission"] = "read"
        with self.assertRaises(Refusal):
            entry.admit(fixtures.request(), fixtures.Store(fixtures.registered()), CONTEXT, api)
        requested = fixtures.request()
        requested["source"]["tree"] = "0" * 40
        with self.assertRaises(Refusal):
            entry.admit(requested, fixtures.Store(fixtures.registered()), CONTEXT, fixtures.provider())

    def test_scoped_reader_has_no_writer_or_generic_credential_fallback(self):
        held = {"WHARF_LANE_READER_ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
                "WHARF_LANE_READER_BUCKET": "lane-state", "WHARF_LANE_READER_ACCESS_KEY_ID": "reader",
                "WHARF_LANE_READER_SECRET_ACCESS_KEY": "private"}
        reader = entry.Reader(fixtures.request(), held)
        for method in ("create", "swap", "put", "copy", "get", "head", "prefixes"):
            self.assertFalse(hasattr(reader, method))
        with mock.patch.object(reader, "connect") as network:
            with self.assertRaises(Refusal):
                reader.snapshot("v1/releases/stable/record.json")
            network.assert_not_called()
        with self.assertRaises(Refusal):
            entry.Reader(fixtures.request(), {key.replace("LANE_READER", "R2"): value for key, value in held.items()})

    def test_build_projection_is_not_a_legacy_envelope_or_authorization(self):
        requested = fixtures.request()
        observed = entry.admit(requested, fixtures.Store(fixtures.registered()), CONTEXT, fixtures.provider())
        projected = entry.source(requested, observed)
        self.assertEqual(projected["schema"], "wharf.lane.build-source/v1")
        self.assertEqual(projected["registration"], requested["registration"])
        changed = copy.deepcopy(observed)
        changed["intent"] = "0" * 64
        with self.assertRaises(Refusal):
            entry.source(requested, changed)

    def test_signed_reader_is_bounded_and_never_treats_errors_as_absence(self):
        held = {"WHARF_LANE_READER_ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
                "WHARF_LANE_READER_BUCKET": "lane-state", "WHARF_LANE_READER_ACCESS_KEY_ID": "reader",
                "WHARF_LANE_READER_SECRET_ACCESS_KEY": "private-reader"}
        reader = entry.Reader(fixtures.request(), held)
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda key: '"version"' if key == "ETag" else None
        response.read.return_value = b"{}"
        reader.connect = mock.Mock(return_value=connection)
        key = registration.location(fixtures.request()["target"])
        self.assertEqual(reader.snapshot(key), (b"{}", '"version"'))
        response.read.assert_called_once_with(65537)
        self.assertEqual(connection.request.call_args.args, ("GET", "/lane-state/" + key))
        self.assertIn("authorization", connection.request.call_args.kwargs["headers"])
        for status in (301, 403, 429, 500):
            response.status = status
            with self.subTest(status=status), self.assertRaises(Refusal):
                reader.snapshot(key)
        response.status = 404
        self.assertEqual(reader.snapshot(key), (None, None))
        response.status = 200
        response.read.return_value = b"x" * 65537
        with self.assertRaises(Refusal):
            reader.snapshot(key)
        connection.close.side_effect = OSError("private-reader")
        with self.assertRaises(Refusal) as error:
            reader.snapshot(key)
        self.assertNotIn("private-reader", str(error.exception))


if __name__ == "__main__":
    unittest.main()
