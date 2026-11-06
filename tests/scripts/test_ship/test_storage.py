from contextlib import ExitStack, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from lib.refusal import Refusal
from lib.store import r2
from scripts import distribution, ship


class Credentials:
    access_key = "0123456789abcdef" * 2

    @property
    def secret_key(self):
        raise AssertionError("diagnostic read the secret")


class Storage(unittest.TestCase):
    def bucket(self):
        return SimpleNamespace(host="a" * 32 + ".r2.cloudflarestorage.com", name="perish-demo-releases", credentials=Credentials())

    def context(self):
        return dict(repository="PerishLab/demo", marker="v1.2.3-rc.1", commit="b" * 40, tree="c" * 40, wharf="d" * 40, run="7", attempt="1")

    def assert_reference(self, output, bucket):
        self.assertEqual(json.loads(output.getvalue()), dict(schema="wharf.store.reference/v1", state="observed", host=bucket.host, bucket=bucket.name, access_sha256=hashlib.sha256(Credentials.access_key.encode()).hexdigest()))
        self.assertNotIn(Credentials.access_key, output.getvalue())

    def test_reference_uses_no_secret_or_io_and_refuses_unknown_metadata_without_echo(self):
        bucket = self.bucket()
        expected = r2.reference(bucket)
        self.assertEqual(expected["access_sha256"], hashlib.sha256(Credentials.access_key.encode()).hexdigest())
        for field, value in (("host", "secret.invalid"), ("name", "secret/invalid"), ("credentials", None)):
            with self.subTest(field=field):
                bucket = self.bucket()
                setattr(bucket, field, value)
                self.assertEqual(r2.reference(bucket), dict(schema="wharf.store.reference/v1", state="unavailable"))

    def test_publication_observes_actual_release_store_before_refusal_and_preserves_success(self):
        for refused in (False, True):
            with self.subTest(refused=refused):
                self.publication(refused)

    def publication(self, refused):
        bucket, output = self.bucket(), io.StringIO()
        context = self.context()
        failure = Refusal("HEAD seal answered 403")
        expected = {"action": "published"}

        def publish(released, contents, store):
            self.assertIs(store, bucket)
            self.assert_reference(output, bucket)
            self.assertEqual(released.commit, context["commit"])
            if refused:
                raise failure
            return expected

        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(output))
            patches = [(ship, "opened", ((object(), dict(context=context, entries={}), {}))), (ship, "declared", {}), (ship, "placements", {}), (ship, "place", Path("/unused")), (ship.releasing, "render", None), (ship.execution, "ready", None)]
            for target, name, result in patches:
                stack.enter_context(mock.patch.object(target, name, return_value=result))
            writer = stack.enter_context(mock.patch.object(ship.r2, "writer", return_value=bucket))
            stack.enter_context(mock.patch.object(ship.releasing, "publish", side_effect=publish))
            if refused:
                with self.assertRaises(Refusal) as error:
                    ship.run_release(dict(context, source="/unused"))
                self.assertIs(error.exception, failure)
            else:
                self.assertIs(ship.run_release(dict(context, source="/unused")), expected)
            writer.assert_called_once_with("perish-demo-releases", "RELEASES")

    def test_recording_observes_actual_release_store_before_refusal_and_preserves_outputs(self):
        for refused in (False, True):
            with self.subTest(refused=refused):
                self.recording(refused)

    def recording(self, refused):
        bucket, output = self.bucket(), io.StringIO()
        context = self.context()
        failure = Refusal("HEAD distribution answered 403")
        expected = {"state": "incomplete"}

        def record(store, observed, needs, fetch):
            self.assertIs(store, bucket)
            self.assert_reference(output, bucket)
            self.assertEqual(observed["commit"], context["commit"])
            if refused:
                raise failure
            return expected

        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(output))
            writer = stack.enter_context(mock.patch.object(distribution.r2, "writer", return_value=bucket))
            stack.enter_context(mock.patch.object(distribution.distribution, "record", side_effect=record))
            answer = stack.enter_context(mock.patch.object(distribution.parameters, "answer"))
            if refused:
                with self.assertRaises(Refusal) as error:
                    distribution.record(dict(context, needs="{}"))
                self.assertIs(error.exception, failure)
                answer.assert_not_called()
            else:
                self.assertIs(distribution.record(dict(context, needs="{}")), expected)
                answer.assert_called_once_with(dict(state="incomplete"))
            writer.assert_called_once_with("perish-demo-releases", "RELEASES")
