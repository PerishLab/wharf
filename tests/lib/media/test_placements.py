import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from lib.media import release
from tests.lib.store.memory import Memory

LINUX = "x86_64-unknown-linux-gnu"
DARWIN = "aarch64-apple-darwin"
WINDOWS = "x86_64-pc-windows-msvc"


def bound(names):
    root = Path(tempfile.mkdtemp())
    held = {}
    for target, listed in names.items():
        directory = root / target
        directory.mkdir()
        suffix = ".exe" if "windows" in target else ""
        for name in listed:
            (directory / f"{name}-{target}{suffix}").write_bytes(f"{name} {target}".encode())
        held[target] = directory
    return held


def managers():
    root = Path(tempfile.mkdtemp())
    (root / "manage.sh").write_text("sh")
    return root


class Sealed(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def publish(self, repository, contents):
        held = release.Release(repository, "v0.2.0-rc.1", "a" * 40, "b" * 40)
        release.publish(held, contents, self.bucket, lambda url: self.bucket.get(url.split(".perish.uk/", 1)[1]))
        return json.loads(self.bucket.get("v1/releases/rc/v0.2.0-rc.1/seal.json"))

    def member(self, entry):
        return self.bucket.get(entry["url"].split(".perish.uk/", 1)[1])

    def test_a_single_executable_product_seals_the_names_and_keys_it_always_had(self):
        installed = {target: ["plumb"] for target in (LINUX, DARWIN, WINDOWS)}
        seal = self.publish("PerishLab/plumb", release.Contents(bound(installed), managers(), installed, {}))
        self.assertEqual(sorted(seal["artifacts"]), ["darwin-arm64", "linux-x64", "windows-x64"])
        names = {key: entry["name"] for key, entry in seal["artifacts"].items()}
        self.assertEqual(names, {"linux-x64": f"plumb-{LINUX}.tar.gz", "darwin-arm64": f"plumb-{DARWIN}.tar.gz", "windows-x64": f"plumb-{WINDOWS}.zip"})
        for entry in seal["artifacts"].values():
            self.assertEqual(entry["url"], f"https://releases.plumb.perish.uk/v1/objects/sha256/{entry['sha256']}/{entry['name']}")
        with tarfile.open(fileobj=io.BytesIO(self.member(seal["artifacts"]["linux-x64"]))) as held:
            self.assertEqual(held.getnames(), ["plumb"])
        self.assertEqual(zipfile.ZipFile(io.BytesIO(self.member(seal["artifacts"]["windows-x64"]))).namelist(), ["plumb.exe"])

    def test_a_platform_archive_holds_only_what_it_installs(self):
        built = {LINUX: ["santi", "santi-api"], DARWIN: ["santi"]}
        installed = {LINUX: ["santi"], DARWIN: ["santi"]}
        seal = self.publish("PerishLab/santi", release.Contents(bound(built), managers(), installed, {}))
        with tarfile.open(fileobj=io.BytesIO(self.member(seal["artifacts"]["linux-x64"]))) as held:
            self.assertEqual(held.getnames(), ["santi"])

    def test_a_platform_that_installs_nothing_has_no_archive(self):
        built = {LINUX: ["santi", "santi-api"], DARWIN: ["santi-api"]}
        seal = self.publish("PerishLab/santi", release.Contents(bound(built), managers(), {LINUX: ["santi", "santi-api"], DARWIN: []}, {}))
        self.assertEqual(sorted(seal["artifacts"]), ["linux-x64"])
        with tarfile.open(fileobj=io.BytesIO(self.member(seal["artifacts"]["linux-x64"]))) as held:
            self.assertEqual(held.getnames(), ["santi", "santi-api"])

    def test_the_deb_enters_the_seal_beside_the_archives(self):
        deb = Path(tempfile.mkdtemp()) / f"santi-api_0.1.0~rc.2_amd64.deb"
        deb.write_bytes(b"!<arch>\n")
        installed = {LINUX: ["santi"]}
        seal = self.publish("PerishLab/santi", release.Contents(bound({LINUX: ["santi", "santi-api"]}), managers(), installed, {"deb": deb}))
        self.assertEqual(sorted(seal["artifacts"]), ["linux-x64", "linux-x64-deb"])
        entry = seal["artifacts"]["linux-x64-deb"]
        digest = hashlib.sha256(b"!<arch>\n").hexdigest()
        self.assertEqual(entry, {"name": f"santi-api_0.1.0~rc.2_amd64.deb", "mime": "application/vnd.debian.binary-package", "sha256": digest, "size": 8, "url": f"https://releases.santi.perish.uk/v1/objects/sha256/{digest}/santi-api_0.1.0~rc.2_amd64.deb"})
        self.assertEqual(self.member(entry), b"!<arch>\n")
        self.assertEqual(release.sealed_targets(seal), [LINUX])


class Managed(unittest.TestCase):
    def render(self, repository, installed, marker="v0.2.0"):
        out = Path(tempfile.mkdtemp()) / "managers"
        release.render(release.Release(repository, marker, "a" * 40, "b" * 40), out, installed)
        return out

    def test_the_unix_manager_installs_each_platform_s_own_executables(self):
        out = self.render("PerishLab/santi", {LINUX: ["santi", "santi-ctl"], DARWIN: ["santi"]})
        body = (out / "manage.sh").read_text()
        self.assertIn(f"ARCHIVE=santi-{LINUX}.tar.gz\n      ARTIFACT=linux-x64\n      ARCHIVE_ROOT=\n      FORMAT=tar.gz\n      BINARIES=\"santi santi-ctl\"\n", body)
        self.assertIn(f"ARCHIVE=santi-{DARWIN}.tar.gz\n      ARTIFACT=darwin-arm64\n      ARCHIVE_ROOT=\n      FORMAT=tar.gz\n      BINARIES=\"santi\"\n", body)
        self.assertIn("\nBINARIES=\n", body)
        self.assertEqual(subprocess.run(["sh", "-n", str(out / "manage.sh")]).returncode, 0)
        self.assertEqual(subprocess.run(["sh", "-n", str(out / release.CANONICAL / "manage.sh")]).returncode, 0)

    def test_a_platform_that_installs_nothing_is_not_offered(self):
        body = (self.render("PerishLab/santi", {LINUX: ["santi"], DARWIN: []}) / "manage.sh").read_text()
        self.assertNotIn("Darwin:arm64", body)

    def test_the_windows_manager_loops_over_its_executables(self):
        body = (self.render("PerishLab/santi", {LINUX: ["santi"], WINDOWS: ["santi", "santi-ctl"]}) / "manage.ps1").read_text()
        self.assertIn("$binaries = @('santi', 'santi-ctl')\n", body)
        self.assertIn("foreach ($name in $binaries)", body)
        self.assertNotIn("santi.exe'", body)

    def test_a_single_executable_product_renders_the_manager_it_always_offered(self):
        out = self.render("PerishLab/plumb", {LINUX: ["plumb"], DARWIN: ["plumb"], WINDOWS: ["plumb"]})
        unix, windows = (out / "manage.sh").read_text(), (out / "manage.ps1").read_text()
        self.assertEqual(unix.count('BINARIES="plumb"'), 2)
        self.assertIn(f"ARCHIVE=plumb-{LINUX}.tar.gz", unix)
        self.assertIn("$binaries = @('plumb')\n", windows)
        self.assertIn(f"$archive = 'plumb-{WINDOWS}.zip'", windows)
        self.assertIn("$seal.artifacts.'windows-x64'.sha256", windows)
        self.assertNotIn("{", "".join(line for line in unix.splitlines() if "BINARIES" in line))
