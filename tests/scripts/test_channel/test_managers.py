import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.media import release
from lib.refusal import Refusal
from tests.lib.store.memory import Memory


class Managers(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.product = release.Release("PerishLab/demo", "v1.2.3", "a" * 40, "b" * 40)
        self.directory = self.rendered({"manage.sh": b"old unix", "manage.ps1": b"old windows"})
        self.bucket.objects[release.sealed(self.product)] = json.dumps(dict(schema=1, product="demo", channel="stable", releaseVersion=self.product.marker, commit=self.product.commit, managers={"unix": {}, "windows": {}})).encode()

    def rendered(self, files):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = directory / release.CANONICAL
        root.mkdir()
        for name, body in files.items():
            (root / name).write_bytes(body)
        return directory

    def point(self, directory=None):
        return release.point(self.product, self.directory if directory is None else directory, self.bucket)

    def published(self):
        self.point()
        self.bucket.writes.clear()
        return json.loads(self.bucket.get(release.pointing(self.product.marker)))

    def changed(self):
        return self.rendered({"manage.sh": b"new unix", "manage.ps1": b"new windows"})

    def rejected(self, directory):
        before = dict(self.bucket.objects)
        with self.assertRaises(Refusal):
            self.point(directory)
        self.assertEqual(self.bucket.objects, before)
        self.assertEqual(self.bucket.writes, [])

    def test_same_marker_keeps_referenced_bytes_even_when_new_renderer_differs(self):
        pointer = self.published()
        result = self.point(self.changed())
        self.assertEqual(result["pointer"], {"pointer": "v1/channels/stable.json", "state": "kept", "current": "v1.2.3", "managers": ["manage.ps1", "manage.sh"]})
        self.assertEqual(self.bucket.writes, [])
        self.assertEqual(pointer, json.loads(self.bucket.get("v1/channels/stable.json")))
        for platform, entry in pointer["managers"].items():
            self.assertEqual(hashlib.sha256(self.bucket.get(release.MANAGERS[platform][0])).hexdigest(), entry["sha256"])

    def test_missing_and_wrong_roots_are_restored_only_from_exact_material(self):
        self.published()
        del self.bucket.objects["manage.sh"]
        self.bucket.objects["manage.ps1"] = b"wrong"
        result = self.point()
        self.assertEqual(result["pointer"]["state"], "kept")
        self.assertEqual(self.bucket.writes, ["manage.sh", "manage.ps1"])
        self.assertEqual(self.bucket.get("manage.sh"), b"old unix")
        self.assertEqual(self.bucket.get("manage.ps1"), b"old windows")
        self.assertEqual(self.bucket.headers["manage.sh"]["Content-Type"], release.MANAGERS["unix"][1])
        self.assertEqual(self.bucket.headers["manage.ps1"]["Content-Type"], release.MANAGERS["windows"][1])

    def test_correct_roots_are_not_rewritten_on_exact_retry(self):
        self.published()
        self.point()
        self.assertEqual(self.bucket.writes, [])

    def test_unavailable_old_rendering_refuses_before_either_platform_is_written(self):
        self.published()
        self.bucket.objects["manage.sh"] = b"wrong unix"
        del self.bucket.objects["manage.ps1"]
        changed = self.rendered({"manage.sh": b"old unix", "manage.ps1": b"new windows"})
        self.rejected(changed)

    def test_even_one_missing_root_cannot_be_replaced_with_new_rendering(self):
        self.published()
        del self.bucket.objects["manage.sh"]
        self.rejected(self.changed())

    def test_all_platform_reads_complete_before_first_write(self):
        self.published()
        self.bucket.objects["manage.sh"] = b"wrong unix"
        self.bucket.objects["manage.ps1"] = b"wrong windows"
        seen = []
        snapshot, put = self.bucket.snapshot, self.bucket.put

        def read(key):
            seen.append(("read", key))
            return snapshot(key)

        def write(key, body, headers=None):
            seen.append(("write", key))
            put(key, body, headers)

        with mock.patch.object(self.bucket, "snapshot", side_effect=read), mock.patch.object(self.bucket, "put", side_effect=write):
            self.point()
        self.assertEqual(seen[-4:], [("read", "manage.sh"), ("read", "manage.ps1"), ("write", "manage.sh"), ("write", "manage.ps1")])

    def test_second_platform_read_failure_leaves_first_unrepaired(self):
        self.published()
        self.bucket.objects["manage.sh"] = b"wrong"
        snapshot = self.bucket.snapshot

        def read(key):
            if key == "manage.ps1":
                raise Refusal("provider read unavailable")
            return snapshot(key)

        with mock.patch.object(self.bucket, "snapshot", side_effect=read):
            self.rejected(self.directory)

    def test_pointer_platform_mismatch_refuses_before_writes(self):
        pointer = self.published()
        for managers in ({}, {"unix": pointer["managers"]["unix"]}, dict(pointer["managers"], unknown={})):
            with self.subTest(managers=managers):
                self.bucket.objects["v1/channels/stable.json"] = json.dumps(dict(pointer, managers=managers)).encode()
                self.rejected(self.directory)

    def test_foreign_or_malformed_reference_refuses_even_with_correct_roots(self):
        pointer = self.published()
        for field, value in (("name", "other"), ("url", "https://foreign/manage.sh"), ("mime", "application/json"), ("sha256", "z" * 64), ("size", True), ("extra", "field")):
            with self.subTest(field=field):
                managers = dict(pointer["managers"], unix=dict(pointer["managers"]["unix"], **{field: value}))
                self.bucket.objects["v1/channels/stable.json"] = json.dumps(dict(pointer, managers=managers)).encode()
                self.rejected(self.directory)

    def test_hash_or_size_mismatch_requires_exact_recovery(self):
        pointer = self.published()
        for field, value in (("sha256", "0" * 64), ("size", 1000)):
            with self.subTest(field=field):
                managers = dict(pointer["managers"], windows=dict(pointer["managers"]["windows"], **{field: value}))
                self.bucket.objects["v1/channels/stable.json"] = json.dumps(dict(pointer, managers=managers)).encode()
                self.rejected(self.directory)

    def test_later_marker_is_left_alone_without_root_reads(self):
        pointer = self.published()
        self.bucket.objects["v1/channels/stable.json"] = json.dumps(dict(pointer, releaseVersion="v1.2.4")).encode()
        snapshot = self.bucket.snapshot
        with mock.patch.object(self.bucket, "snapshot", side_effect=lambda key: snapshot(key) if key.startswith("v1/") else self.fail("read another marker's manager")):
            self.assertEqual(self.point(self.changed())["pointer"]["state"], "kept")
        self.assertEqual(self.bucket.writes, [])

    def test_partial_write_failure_can_resume_using_exact_material(self):
        put = self.bucket.put

        def write(key, body, headers=None):
            if key == "manage.ps1":
                raise Refusal("second platform write failed")
            put(key, body, headers)

        with mock.patch.object(self.bucket, "put", side_effect=write), self.assertRaises(Refusal):
            self.point()
        self.assertTrue(self.bucket.exists("v1/channels/stable.json"))
        self.assertTrue(self.bucket.exists("manage.sh"))
        self.assertFalse(self.bucket.exists("manage.ps1"))
        self.bucket.writes.clear()
        self.assertEqual(self.point()["pointer"]["state"], "kept")
        self.assertEqual(self.bucket.writes, ["manage.ps1"])

    def test_native_snapshot_read_failures_cannot_be_treated_as_missing_roots(self):
        from lib.store import r2
        self.published()
        protocol = object.__new__(r2.Bucket)
        protocol.request = mock.Mock(return_value=(503, b"", {}))
        snapshot = self.bucket.snapshot
        with mock.patch.object(self.bucket, "snapshot", side_effect=lambda key: protocol.snapshot(key) if key == "manage.ps1" else snapshot(key)):
            self.rejected(self.directory)
        protocol.request.assert_called_once_with(r2.Operation("GET", "manage.ps1"))

    def test_forged_candidate_metadata_cannot_write_unmatched_bytes(self):
        pointer = self.published()
        del self.bucket.objects["manage.sh"]
        rooted = release.scripts(self.directory / release.CANONICAL, "https://releases.demo.perish.uk", True)
        rooted["unix"] = ("manage.sh", b"wrong bytes", pointer["managers"]["unix"])
        with self.assertRaises(Refusal):
            release.lead(self.bucket, rooted, {"pointer": "v1/channels/stable.json"}, "v1.2.3")
        self.assertEqual(self.bucket.writes, [])
