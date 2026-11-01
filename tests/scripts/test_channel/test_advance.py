import json
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from lib import parameters
from lib.refusal import Conflict, Refusal
from lib.store import r2
from scripts import channel
from tests.lib.store.memory import Memory

HELD = {"repository": "PerishLab/plumb", "marker": "v0.38.0", "wharf": "c" * 40}


class Advance(unittest.TestCase):
    def protocol(self, response, status=200):
        bucket = object.__new__(r2.Bucket)
        bucket.request = mock.Mock(side_effect=[response, (status, b"", {})])
        return bucket

    def test_absent_pointer_is_created_conditionally(self):
        bucket = self.protocol((404, b"", {}))
        body = b'{"releaseVersion":"v1.1.0"}'
        result = channel.release.advance(bucket, "pointer", body, "v1.1.0")
        self.assertEqual(result, {"pointer": "pointer", "state": "advanced"})
        calls = [call.args[0] for call in bucket.request.call_args_list]
        self.assertEqual(calls, [r2.Operation("GET", "pointer"), r2.Operation("PUT", "pointer", body, {"Content-Type": "application/json", "If-None-Match": "*"})])

    def test_an_upgrade_uses_the_etag_from_the_same_snapshot(self):
        bucket = self.protocol((200, b'{"releaseVersion":"v1.0.0"}', {"etag": '"observed"'}))
        body = b'{"releaseVersion":"v1.1.0"}'
        self.assertEqual(channel.release.advance(bucket, "pointer", body, "v1.1.0")["state"], "advanced")
        calls = [call.args[0] for call in bucket.request.call_args_list]
        self.assertEqual(calls, [r2.Operation("GET", "pointer"), r2.Operation("PUT", "pointer", body, {"Content-Type": "application/json", "If-Match": '"observed"'})])

    def test_equal_and_newer_markers_need_no_write(self):
        for marker in ("v1.1.0", "v1.2.0"):
            bucket = self.protocol((200, json.dumps({"releaseVersion": marker}).encode(), {"etag": "etag"}))
            with self.subTest(marker=marker):
                self.assertEqual(channel.release.advance(bucket, "pointer", b"unchanged", "v1.1.0"), {"pointer": "pointer", "state": "kept", "current": marker})
                self.assertEqual(bucket.request.call_args_list, [mock.call(r2.Operation("GET", "pointer"))])

    def race(self, initial):
        bucket = Memory()
        if initial is not None:
            bucket.put("pointer", json.dumps({"releaseVersion": initial}).encode())
        newer = b'{"releaseVersion":"v1.2.0"}'
        snapshot = bucket.snapshot

        def observe(key):
            held = snapshot(key)
            bucket.put(key, newer)
            return held

        with mock.patch.object(bucket, "snapshot", side_effect=observe), self.assertRaises(Conflict):
            channel.release.advance(bucket, "pointer", b'{"releaseVersion":"v1.1.0"}', "v1.1.0")
        self.assertEqual(bucket.get("pointer"), newer)
        self.assertEqual(bucket.writes.count("pointer"), 1 if initial is None else 2)

    def test_a_concurrent_upgrade_cannot_be_overwritten(self):
        self.race("v1.0.0")

    def test_a_concurrent_creation_cannot_be_overwritten(self):
        self.race(None)

    def test_provider_conflicts_are_not_retried_or_downgraded(self):
        for response in ((404, b"", {}), (200, b'{"releaseVersion":"v1.0.0"}', {"etag": "etag"})):
            bucket = self.protocol(response, 412)
            with self.subTest(response=response), self.assertRaises(Conflict):
                channel.release.advance(bucket, "pointer", b'{"releaseVersion":"v1.1.0"}', "v1.1.0")
            self.assertEqual([call.args[0].method for call in bucket.request.call_args_list], ["GET", "PUT"])

    def test_unavailable_or_unversioned_reads_cannot_become_creation(self):
        for response in ((500, b"", {}), (200, b'{"releaseVersion":"v1.0.0"}', {})):
            bucket = self.protocol(response)
            with self.subTest(response=response), self.assertRaises(Refusal):
                channel.release.advance(bucket, "pointer", b'{"releaseVersion":"v1.1.0"}', "v1.1.0")
            self.assertEqual(bucket.request.call_args_list, [mock.call(r2.Operation("GET", "pointer"))])

    def test_other_write_failures_do_not_retry(self):
        bucket = self.protocol((404, b"", {}), 500)
        with self.assertRaises(Refusal):
            channel.release.advance(bucket, "pointer", b'{"releaseVersion":"v1.1.0"}', "v1.1.0")
        self.assertEqual(bucket.request.call_count, 2)

    def test_channel_reports_a_conflict_and_stops_before_managers(self):
        source = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (source / "plumb.toml").write_text('[release]\nbinaries = ["plumb"]\ntargets = ["x86_64-unknown-linux-gnu"]\n')
        held = dict(HELD, source=str(source))
        bucket = Memory()
        bucket.put(channel.release.sealed(channel.released(held)), json.dumps(dict(schema=1, product="plumb", channel="stable", releaseVersion=HELD["marker"], commit="a" * 40, artifacts={}, managers={"unix": {}})).encode())
        with mock.patch.object(channel.publication, "read", return_value=dict(HELD, commit="a" * 40)), mock.patch.object(bucket, "swap", side_effect=Conflict("pointer changed")) as swap, mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.parameters, "resolve", return_value=(held, {})), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(channel.main(["point"]), 2)
        self.assertIn("pointer changed", errors.getvalue())
        swap.assert_called_once()
        self.assertEqual(len(bucket.writes), 1)


