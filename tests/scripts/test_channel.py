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
        bucket.put(channel.release.sealed(channel.released(held)), b'{"commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","artifacts":{},"managers":{"unix":{}}}')
        with mock.patch.object(bucket, "swap", side_effect=Conflict("pointer changed")) as swap, mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.parameters, "resolve", return_value=(held, {})), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(channel.main(["point"]), 2)
        self.assertIn("pointer changed", errors.getvalue())
        swap.assert_called_once()
        self.assertEqual(len(bucket.writes), 1)


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in channel.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())


class Plan(unittest.TestCase):
    def decide(self, overtaken):
        output = Path(tempfile.mkdtemp()) / "output"
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(output)}), mock.patch.object(channel.release, "overtaken", return_value=overtaken), mock.patch.object(channel.release, "carried", return_value=True):
            held = channel.plan(dict(HELD, source="../product"))
        return held, output.read_text()

    def test_a_product_with_no_binaries_has_no_channel_to_point(self):
        output = Path(tempfile.mkdtemp()) / "output"
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(output)}), mock.patch.object(channel.release, "carried", return_value=False):
            held = channel.plan(dict(HELD, source="../product"))
        self.assertEqual(held, {"decision": "skip", "presence": "none", "channel": "stable"})

    def test_a_channel_not_yet_at_a_newer_marker_is_pointed(self):
        self.assertEqual(self.decide(False), ({"decision": "run", "presence": "present", "channel": "stable"}, "decision=run\npresence=present\n"))

    def test_a_channel_already_at_a_newer_marker_is_left_alone(self):
        self.assertEqual(self.decide(True)[1], "decision=skip\npresence=overtaken\n")


class Point(unittest.TestCase):
    def test_the_pointer_is_handed_managers_rendered_for_its_marker(self):
        seen = {}

        def point(release, managers, bucket):
            seen.update(marker=release.marker, wharf=release.wharf, files=sorted(path.name for path in Path(managers).rglob("*") if path.is_file()))
            return {"pointer": "moved"}

        bucket = mock.Mock()
        bucket.get.return_value = json.dumps({"artifacts": {key: {} for key in ("linux-x64", "darwin-arm64", "windows-x64")}}).encode()
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text('[release]\nbinaries = ["plumb"]\ntargets = ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"]\n')
        with mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.release, "point", side_effect=point):
            self.assertEqual(channel.point(dict(HELD, source=str(source))), {"pointer": "moved"})
        self.assertEqual(seen, {"marker": "v0.38.0", "wharf": "c" * 40, "files": ["manage.ps1", "manage.ps1", "manage.sh", "manage.sh"]})

    def test_the_canonical_managers_install_what_the_product_declares_for_each_sealed_platform(self):
        rendered = {}
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text('[release]\nbinaries = ["santi", "santi-api"]\ntargets = ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin"]\n[release.binary.santi-api]\ninstall = false\n')
        bucket = mock.Mock()
        bucket.get.return_value = json.dumps({"artifacts": {"linux-x64": {}, "linux-x64-deb": {}}}).encode()
        with mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.release, "render", side_effect=lambda release, output, installed, source: rendered.update(installed)), mock.patch.object(channel.release, "point", return_value={}):
            channel.point(dict(HELD, repository="PerishLab/santi", source=str(source)))
        self.assertEqual(rendered, {"x86_64-unknown-linux-gnu": ["santi"]})
