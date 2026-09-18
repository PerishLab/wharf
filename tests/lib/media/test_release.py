import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from lib.media import archive, release
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

SEAL_FIELDS = {"schema", "product", "channel", "releaseVersion", "commit", "url", "generator", "provenance", "artifacts", "managers"}
REMOTE_FIELDS = {"name", "mime", "sha256", "size", "url"}


def bound():
    root = Path(tempfile.mkdtemp())
    held = {}
    for target in release.LAYOUT["targets"]:
        directory = root / target
        directory.mkdir()
        suffix = ".exe" if "windows" in target else ""
        (directory / f"plumb-{target}{suffix}").write_bytes(f"binary {target}".encode())
        held[target] = directory
    return held


class Archive(unittest.TestCase):
    def test_tarball_is_deterministic_and_executable(self):
        body = archive.tarball("plumb", b"elf")
        self.assertEqual(body, archive.tarball("plumb", b"elf"))
        with tarfile.open(fileobj=io.BytesIO(body)) as held:
            entry = held.getmember("plumb")
            self.assertEqual((entry.mode, entry.mtime, entry.uid), (0o755, 0, 0))
            self.assertEqual(held.extractfile(entry).read(), b"elf")

    def test_zipball_is_deterministic(self):
        body = archive.zipball("plumb.exe", b"pe")
        self.assertEqual(body, archive.zipball("plumb.exe", b"pe"))
        self.assertEqual(zipfile.ZipFile(io.BytesIO(body)).read("plumb.exe"), b"pe")


class Publish(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.bound = bound()
        self.served = {}

    def reader(self, url):
        key = url.split(".perish.uk/", 1)[1]
        return self.bucket.get(key)

    def publish(self, marker):
        return release.publish(release.Release("PerishLab/plumb", marker, "a" * 40, "b" * 40), self.bound, self.bucket, self.reader)

    def test_writes_the_v1_layout_plumb_can_read(self):
        result = self.publish("v0.38.0-beta.7")
        self.assertEqual(result["state"], "published")
        seal = json.loads(self.bucket.get("v1/releases/beta/v0.38.0-beta.7/seal.json"))
        self.assertEqual(set(seal), SEAL_FIELDS)
        self.assertEqual(sorted(seal["artifacts"]), ["darwin-arm64", "linux-x64", "windows-x64"])
        for entry in seal["artifacts"].values():
            self.assertEqual(set(entry), REMOTE_FIELDS)
            key = entry["url"].split(".perish.uk/", 1)[1]
            self.assertTrue(key.startswith(f"v1/objects/sha256/{entry['sha256']}/"))
        self.assertEqual(seal["generator"]["origin"]["kind"], "source-built")
        pointer = json.loads(self.bucket.get("v1/channels/beta.json"))
        self.assertEqual((pointer["releaseVersion"], set(pointer["seal"])), ("v0.38.0-beta.7", REMOTE_FIELDS))
        self.assertIn('\n  "artifacts": {\n    "darwin-arm64": {\n      "name"', self.bucket.get("v1/releases/beta/v0.38.0-beta.7/seal.json").decode())

    def test_pointer_only_moves_forward(self):
        self.publish("v0.38.0-beta.7")
        older = self.publish("v0.38.0-beta.6")
        self.assertEqual(older["pointer"]["state"], "kept")
        self.assertEqual(json.loads(self.bucket.get("v1/channels/beta.json"))["releaseVersion"], "v0.38.0-beta.7")

    def test_republishing_is_idempotent_and_releases_are_immutable(self):
        self.publish("v0.38.0-beta.7")
        self.assertEqual(self.publish("v0.38.0-beta.7")["state"], "already-published")
        (self.bound["x86_64-unknown-linux-gnu"] / "plumb-x86_64-unknown-linux-gnu").write_bytes(b"other")
        with self.assertRaises(Refusal):
            self.publish("v0.38.0-beta.7")

    def test_never_writes_stable(self):
        with self.assertRaises(Refusal):
            self.publish("v0.38.0")
        self.assertEqual(self.bucket.writes, [])
