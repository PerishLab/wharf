import hashlib
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


def managers(stable):
    root = Path(tempfile.mkdtemp())
    for directory in [root, root / release.CANONICAL] if stable else [root]:
        directory.mkdir(exist_ok=True)
        (directory / "manage.sh").write_text(f"sh {directory.name}")
        (directory / "manage.ps1").write_text(f"ps1 {directory.name}")
    return root


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

    def publish(self, marker, held=None):
        held = managers(release.channel(marker) == "stable") if held is None else held
        return release.publish(release.Release("PerishLab/plumb", marker, "a" * 40, "b" * 40), release.Contents(self.bound, held), self.bucket, self.reader)

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

    def test_a_prerelease_seal_carries_its_pinned_managers_and_leaves_the_root_alone(self):
        self.publish("v0.38.0-rc.2")
        seal = json.loads(self.bucket.get("v1/releases/rc/v0.38.0-rc.2/seal.json"))
        self.assertEqual(sorted(seal["managers"]), ["unix", "windows"])
        unix = seal["managers"]["unix"]
        self.assertEqual((unix["name"], unix["mime"]), ("manage.sh", "text/x-shellscript; charset=utf-8"))
        self.assertTrue(self.bucket.get(unix["url"].split(".perish.uk/", 1)[1]).startswith(b"sh "))
        self.assertEqual(json.loads(self.bucket.get("v1/channels/rc.json"))["managers"], {})
        self.assertFalse(self.bucket.exists("manage.sh"))

    def test_stable_moves_the_canonical_managers_with_its_pointer(self):
        result = self.publish("v0.38.0")
        self.assertEqual(result["pointer"]["managers"], ["manage.ps1", "manage.sh"])
        self.assertEqual(self.bucket.get("manage.sh"), b"sh canonical")
        pointer = json.loads(self.bucket.get("v1/channels/stable.json"))
        self.assertEqual(pointer["managers"]["unix"]["url"], "https://releases.plumb.perish.uk/manage.sh")
        self.assertEqual(pointer["managers"]["windows"]["sha256"], hashlib.sha256(b"ps1 canonical").hexdigest())
        seal = json.loads(self.bucket.get("v1/releases/stable/v0.38.0/seal.json"))
        self.assertNotEqual(seal["managers"]["unix"]["url"], pointer["managers"]["unix"]["url"])

    def test_an_older_stable_keeps_the_root_managers(self):
        self.publish("v0.38.1")
        self.bucket.put("manage.sh", b"newer")
        self.assertEqual(self.publish("v0.38.0")["pointer"]["state"], "kept")
        self.assertEqual(self.bucket.get("manage.sh"), b"newer")

    def test_a_rerun_stable_restores_its_root_managers(self):
        self.publish("v0.38.0")
        self.bucket.put("manage.sh", b"lost")
        self.assertEqual(self.publish("v0.38.0")["state"], "already-published")
        self.assertEqual(self.bucket.get("manage.sh"), b"sh canonical")

    def test_refuses_stable_without_canonical_managers(self):
        with self.assertRaisesRegex(Refusal, "manager scripts"):
            self.publish("v0.38.0", managers(False))
        self.assertEqual(self.bucket.writes, [])

    def test_rc_writes_its_own_channel(self):
        self.publish("v0.38.0-rc.1")
        self.assertEqual(json.loads(self.bucket.get("v1/channels/rc.json"))["releaseVersion"], "v0.38.0-rc.1")
        self.assertFalse(self.bucket.exists("v1/channels/beta.json"))


class Render(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.binary = self.root / "plumb"
        self.binary.write_text('#!/bin/sh\n[ "$1 $2 $3" = "release managers --version" ] || exit 3\nmkdir "$6" && printf "%s %s " "$4" "$HOME" > "$6/manage.sh" && pwd >> "$6/manage.sh"\n')

    def test_runs_the_declared_step_in_a_clean_home_at_the_source(self):
        held = release.Release("PerishLab/plumb", "v0.38.0-rc.2", "a" * 40, "b" * 40)
        result = release.render(held, self.binary, self.root, self.root / "out")
        self.assertEqual(result["files"], ["manage.sh"])
        body = (self.root / "out" / "manage.sh").read_text()
        self.assertTrue(body.startswith("v0.38.0-rc.2 /"))
        self.assertNotIn(str(Path.home()), body.split()[1])
        self.assertEqual(body.split()[2], str(self.root.resolve()))

    def test_an_undeclared_product_renders_nothing(self):
        held = release.Release("PerishLab/other", "v1.0.0", "a" * 40, "b" * 40)
        self.assertIsNone(release.render(held, self.binary, self.root, self.root / "out")["step"])
        self.assertEqual(list((self.root / "out").iterdir()), [])

    def test_a_failing_step_refuses(self):
        self.binary.write_text("#!/bin/sh\necho broken >&2\nexit 4\n")
        held = release.Release("PerishLab/plumb", "v1.0.0", "a" * 40, "b" * 40)
        with self.assertRaisesRegex(Refusal, "exited 4: broken"):
            release.render(held, self.binary, self.root, self.root / "out")


class Order(unittest.TestCase):
    def test_channels_and_version_order(self):
        self.assertEqual([release.channel(marker) for marker in ("v1.0.0", "v1.0.0-rc.2", "v1.0.0-beta.3")], ["stable", "rc", "beta"])
        self.assertLess(release.order("v1.0.0-beta.9"), release.order("v1.0.0-rc.1"))
        self.assertLess(release.order("v1.0.0-rc.9"), release.order("v1.0.0"))
        self.assertLess(release.order("v1.0.0"), release.order("v1.0.1-beta.1"))
        with self.assertRaises(Refusal):
            release.channel("1.0.0")
