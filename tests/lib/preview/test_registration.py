import copy
import http.client
import unittest
from unittest import mock

from lib.content import canonical
from lib.content.static import admission
from lib.refusal import Refusal
from lib.store import r2
from tests.lib.content.test_preview import target
from tests.lib.preview.test_admission import Provider, request, CONTEXT

CONFIGURATION = {"endpoint": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "bucket": "preview-register",
                 "access": "private-reader-id", "secret": "private-reader-key"}
SELECTION = {"repository": "PerishLab/crest", "app": "crest-review"}
KEY = "preview/v1/registrations/PerishLab/crest/crest-review.json"


class Registration(unittest.TestCase):
    def reader(self, body=None):
        reader = r2.Registration(copy.deepcopy(CONFIGURATION), dict(SELECTION))
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = canonical.encode(target()) if body is None else body
        reader.connect = mock.Mock(return_value=connection)
        return reader, connection

    def test_exact_signed_get_bounded_without_writes(self):
        reader, connection = self.reader()
        self.assertEqual(reader.get(KEY), canonical.encode(target()))
        reader.connect.assert_called_once_with("a" * 32 + ".r2.cloudflarestorage.com", timeout=30)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/preview-register/" + KEY))
        self.assertTrue(kwargs["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 "))
        connection.getresponse.return_value.read.assert_called_once_with(65537)
        connection.close.assert_called_once()
        for name in ("create", "swap", "put", "copy", "head", "snapshot", "request", "prefixes"):
            self.assertFalse(hasattr(reader, name))
        self.assertNotIn(CONFIGURATION["secret"], repr(reader))

    def test_foreign_keys_refuse_before_network(self):
        for key in ("preview/v1/registrations/PerishLab/design/crest-review.json", KEY + "?x=1", "release/v1/test", KEY.replace("crest-review", "../worker")):
            reader, connection = self.reader()
            with self.subTest(key=key), self.assertRaises(Refusal):
                reader.get(key)
            reader.connect.assert_not_called()
            connection.request.assert_not_called()

    def test_invalid_origins_credentials_and_selections_refuse(self):
        changes = {"endpoint": ["http://example.com", CONFIGURATION["endpoint"] + "/", CONFIGURATION["endpoint"] + ":443", CONFIGURATION["endpoint"].replace(".r2.", "xr2x"), "https://user@" + CONFIGURATION["endpoint"][8:]],
                   "bucket": ["../release", "bad/name", "", "a" * 64], "access": ["", "line\nbreak"], "secret": ["", None]}
        for key, values in changes.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(Refusal):
                    r2.Registration(dict(CONFIGURATION, **{key: value}), SELECTION)
        for selection in (dict(SELECTION, repository="foreign/crest"), dict(SELECTION, app="../worker"), dict(SELECTION, endpoint="injected")):
            with self.subTest(selection=selection), self.assertRaises(Refusal):
                r2.Registration(CONFIGURATION, selection)

    def test_non_200_oversized_and_network_refuse_without_retry(self):
        for status in (301, 302, 403, 404, 429, 500):
            reader, connection = self.reader()
            connection.getresponse.return_value.status = status
            with self.subTest(status=status), self.assertRaises(Refusal):
                reader.get(KEY)
            connection.getresponse.return_value.read.assert_not_called()
            reader.connect.assert_called_once()
            connection.close.assert_called_once()
        reader, connection = self.reader(b"x" * 65537)
        with self.assertRaises(Refusal):
            reader.get(KEY)
        for failure in (TimeoutError("private-reader-key"), OSError("private-reader-key"), http.client.HTTPException("private-reader-key")):
            reader, connection = self.reader()
            connection.request.side_effect = failure
            with self.subTest(failure=failure), self.assertRaises(Refusal) as observed:
                reader.get(KEY)
            self.assertNotIn(CONFIGURATION["secret"], str(observed.exception))
            reader.connect.assert_called_once()
            connection.close.assert_called_once()

    def test_transport_construction_and_close_failure_are_generic(self):
        reader, connection = self.reader()
        reader.connect.side_effect = OSError("private-reader-key")
        with self.assertRaises(Refusal) as observed:
            reader.get(KEY)
        self.assertNotIn(CONFIGURATION["secret"], str(observed.exception))
        reader, connection = self.reader()
        connection.close.side_effect = OSError("private-reader-key")
        with self.assertRaises(Refusal) as observed:
            reader.get(KEY)
        self.assertNotIn(CONFIGURATION["secret"], str(observed.exception))

    def test_malformed_documents_fail_existing_admission_before_provider(self):
        for body in (b"{}", b'{"x":1,"x":2}', b"invalid"):
            reader, connection = self.reader(body)
            provider = Provider()
            with self.subTest(body=body), self.assertRaises(Refusal):
                admission.admit(request(), reader, CONTEXT, provider)
            self.assertFalse(provider.calls)

    def test_reader_factory_never_falls_back_to_generic_store_credentials(self):
        generic = {"WHARF_R2_ENDPOINT": CONFIGURATION["endpoint"], "WHARF_R2_BUCKET": CONFIGURATION["bucket"],
                   "WHARF_R2_ACCESS_KEY_ID": CONFIGURATION["access"], "WHARF_R2_SECRET_ACCESS_KEY": CONFIGURATION["secret"]}
        with self.assertRaises(Refusal):
            r2.registration(SELECTION, generic)
        names = {"endpoint": "ENDPOINT", "bucket": "BUCKET", "access": "ACCESS_KEY_ID", "secret": "SECRET_ACCESS_KEY"}
        held = {"WHARF_PREVIEW_REGISTRATION_" + names[key]: value for key, value in CONFIGURATION.items()}
        reader = r2.registration(SELECTION, held)
        self.assertEqual(reader._key, KEY)


if __name__ == "__main__":
    unittest.main()
