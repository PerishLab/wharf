import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

from lib.content import resources
from lib.media import archive, release
from lib.media.release import CANONICAL
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

    def reader(self, url):
        key = url.split(".perish.uk/", 1)[1]
        return self.bucket.get(key)

    def served(self, url):
        if not self.bucket.exists(url.split(".perish.uk/", 1)[1]):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.reader(url)

    def publish(self, marker, held=None):
        held = managers(release.channel(marker) == "stable") if held is None else held
        return release.publish(release.Release("PerishLab/plumb", marker, "a" * 40, "b" * 40), release.Contents(self.bound, held), self.bucket, self.reader)

    def point(self, marker, held=None):
        held = managers(release.channel(marker) == "stable") if held is None else held
        return release.point(release.Release("PerishLab/plumb", marker, "", "b" * 40), held, self.bucket)

    def ship(self, marker):
        return {**self.publish(marker), "pointer": self.point(marker)["pointer"]}

    def test_writes_the_v1_layout_plumb_can_read(self):
        result = self.ship("v0.38.0-beta.7")
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
        self.assertEqual(pointer["commit"], "a" * 40)
        self.assertIn('\n  "artifacts": {\n    "darwin-arm64": {\n      "name"', self.bucket.get("v1/releases/beta/v0.38.0-beta.7/seal.json").decode())

    def test_publishing_leaves_the_channel_to_the_pointer(self):
        self.publish("v0.38.0-beta.7")
        self.assertFalse(self.bucket.exists("v1/channels/beta.json"))

    def test_pointer_only_moves_forward(self):
        self.ship("v0.38.0-beta.7")
        older = self.ship("v0.38.0-beta.6")
        self.assertEqual(older["pointer"]["state"], "kept")
        self.assertEqual(json.loads(self.bucket.get("v1/channels/beta.json"))["releaseVersion"], "v0.38.0-beta.7")

    def test_republishing_is_idempotent_and_releases_are_immutable(self):
        self.publish("v0.38.0-beta.7")
        self.assertEqual(self.publish("v0.38.0-beta.7")["state"], "already-published")
        (self.bound["x86_64-unknown-linux-gnu"] / "plumb-x86_64-unknown-linux-gnu").write_bytes(b"other")
        with self.assertRaises(Refusal):
            self.publish("v0.38.0-beta.7")

    def test_a_prerelease_seal_carries_its_pinned_managers_and_leaves_the_root_alone(self):
        self.ship("v0.38.0-rc.2")
        seal = json.loads(self.bucket.get("v1/releases/rc/v0.38.0-rc.2/seal.json"))
        self.assertEqual(sorted(seal["managers"]), ["unix", "windows"])
        unix = seal["managers"]["unix"]
        self.assertEqual((unix["name"], unix["mime"]), ("manage.sh", "text/x-shellscript; charset=utf-8"))
        self.assertTrue(self.bucket.get(unix["url"].split(".perish.uk/", 1)[1]).startswith(b"sh "))
        self.assertEqual(json.loads(self.bucket.get("v1/channels/rc.json"))["managers"], {})
        self.assertFalse(self.bucket.exists("manage.sh"))

    def test_stable_moves_the_canonical_managers_with_its_pointer(self):
        result = self.ship("v0.38.0")
        self.assertEqual(result["pointer"]["managers"], ["manage.ps1", "manage.sh"])
        self.assertEqual(self.bucket.get("manage.sh"), b"sh canonical")
        pointer = json.loads(self.bucket.get("v1/channels/stable.json"))
        self.assertEqual(pointer["managers"]["unix"]["url"], "https://releases.plumb.perish.uk/manage.sh")
        self.assertEqual(pointer["managers"]["windows"]["sha256"], hashlib.sha256(b"ps1 canonical").hexdigest())
        seal = json.loads(self.bucket.get("v1/releases/stable/v0.38.0/seal.json"))
        self.assertNotEqual(seal["managers"]["unix"]["url"], pointer["managers"]["unix"]["url"])

    def test_an_older_stable_keeps_the_root_managers(self):
        self.ship("v0.38.1")
        self.bucket.put("manage.sh", b"newer")
        self.assertEqual(self.ship("v0.38.0")["pointer"]["state"], "kept")
        self.assertEqual(self.bucket.get("manage.sh"), b"newer")

    def test_a_rerun_stable_restores_its_root_managers(self):
        self.ship("v0.38.0")
        self.bucket.put("manage.sh", b"lost")
        self.assertEqual(self.ship("v0.38.0")["state"], "already-published")
        self.assertEqual(self.bucket.get("manage.sh"), b"sh canonical")

    def test_a_seal_whose_pointer_never_moved_is_pointed_at_later(self):
        self.publish("v0.38.0-rc.1")
        self.assertEqual(self.publish("v0.38.0-rc.1")["state"], "already-published")
        self.assertEqual(self.point("v0.38.0-rc.1")["pointer"]["state"], "advanced")
        self.assertEqual(json.loads(self.bucket.get("v1/channels/rc.json"))["releaseVersion"], "v0.38.0-rc.1")

    def test_pointing_refuses_a_marker_with_no_seal(self):
        with self.assertRaisesRegex(Refusal, "published seal"):
            self.point("v0.38.0-rc.1")
        self.assertEqual(self.bucket.writes, [])

    def test_refuses_stable_without_pinned_managers(self):
        with self.assertRaisesRegex(Refusal, "manager scripts"):
            self.publish("v0.38.0", Path(tempfile.mkdtemp()))
        self.assertEqual(self.bucket.writes, [])

    def test_pointing_refuses_stable_without_canonical_managers(self):
        self.publish("v0.38.0", managers(False))
        with self.assertRaisesRegex(Refusal, "manager scripts"):
            self.point("v0.38.0", managers(False))
        self.assertFalse(self.bucket.exists("v1/channels/stable.json"))

    def test_a_channel_is_overtaken_only_by_a_newer_marker(self):
        pointed = lambda marker: release.overtaken(release.Release("PerishLab/plumb", marker, "", ""), self.served)
        self.assertFalse(pointed("v0.38.0-rc.2"))
        self.ship("v0.38.0-rc.2")
        self.assertEqual([pointed(held) for held in ("v0.38.0-rc.1", "v0.38.0-rc.2", "v0.38.0-rc.3")], [True, False, False])

    def test_rc_writes_its_own_channel(self):
        self.ship("v0.38.0-rc.1")
        self.assertEqual(json.loads(self.bucket.get("v1/channels/rc.json"))["releaseVersion"], "v0.38.0-rc.1")
        self.assertFalse(self.bucket.exists("v1/channels/beta.json"))


class Render(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def render(self, repository, marker):
        held = release.Release(repository, marker, "a" * 40, "b" * 40)
        out = self.root / marker
        return release.render(held, out), out

    def test_a_prerelease_carries_the_pinned_scripts_alone(self):
        result, out = self.render("PerishLab/concord", "v0.12.9-rc.2")
        self.assertEqual(result["files"], ["manage.ps1", "manage.sh"])
        self.assertIn("VERSION=${CONCORD_VERSION:-v0.12.9-rc.2}", (out / "manage.sh").read_text())
        self.assertIn("CHANNEL=${CONCORD_CHANNEL:-rc}", (out / "manage.sh").read_text())

    def test_a_stable_carries_the_canonical_scripts_beside_the_pinned_ones(self):
        result, out = self.render("PerishLab/concord", "v0.12.9")
        self.assertEqual(result["files"], ["canonical/manage.ps1", "canonical/manage.sh", "manage.ps1", "manage.sh"])
        self.assertIn("VERSION=${CONCORD_VERSION:-v0.12.9}", (out / "manage.sh").read_text())
        self.assertIn("VERSION=${CONCORD_VERSION:-}", (out / CANONICAL / "manage.sh").read_text())
        self.assertIn("CHANNEL=${CONCORD_CHANNEL:-stable}", (out / CANONICAL / "manage.sh").read_text())

    def test_the_product_it_names_is_the_one_being_released(self):
        _, out = self.render("PerishLab/concord", "v0.12.9")
        body = (out / "manage.sh").read_text()
        self.assertIn('BINARIES="concord"', body)
        self.assertIn("https://releases.concord.perish.uk", body)
        self.assertNotIn("releases.plumb.perish.uk", body)

    def test_it_offers_the_platforms_this_repository_publishes_and_no_others(self):
        _, out = self.render("PerishLab/concord", "v0.12.9")
        body = (out / "manage.sh").read_text()
        for target, spec in release.LAYOUT["targets"].items():
            for system in spec["systems"]:
                self.assertEqual(system in body, not system.startswith("Windows:"), system)
            self.assertEqual(f"concord-{target}.{spec['format']}" in body, spec["format"] != "zip", target)
        self.assertNotIn("darwin-x64", body)

    def test_the_unix_script_is_executable_and_the_windows_one_is_not(self):
        _, out = self.render("PerishLab/concord", "v0.12.9")
        self.assertTrue(os.access(out / "manage.sh", os.X_OK))
        self.assertFalse(os.access(out / "manage.ps1", os.X_OK))

    def test_an_output_that_exists_refuses(self):
        (self.root / "v1.0.0").mkdir()
        with self.assertRaisesRegex(Refusal, "already exists"):
            self.render("PerishLab/concord", "v1.0.0")

    def test_what_the_seal_calls_its_template_covers_the_scripts_it_publishes(self):
        held = release.implementation.resourced(["lib.media.release"], ["releases.json", *release.TEMPLATES.values()])
        for name in release.TEMPLATES.values():
            self.assertEqual(held[f"resource:{name}"], hashlib.sha256(resources.read_bytes(name)).hexdigest())
        self.assertEqual(release.generator(release.Release("PerishLab/concord", "v0.12.9", "a" * 40, "b" * 40))["template"], release.canonical.digest(held))


class Order(unittest.TestCase):
    def test_channels_and_version_order(self):
        self.assertEqual([release.channel(marker) for marker in ("v1.0.0", "v1.0.0-rc.2", "v1.0.0-beta.3")], ["stable", "rc", "beta"])
        self.assertLess(release.order("v1.0.0-beta.9"), release.order("v1.0.0-rc.1"))
        self.assertLess(release.order("v1.0.0-rc.9"), release.order("v1.0.0"))
        self.assertLess(release.order("v1.0.0"), release.order("v1.0.1-beta.1"))
        with self.assertRaises(Refusal):
            release.channel("1.0.0")
