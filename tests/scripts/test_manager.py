import io
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lib.refusal import Refusal
from scripts import manager
from lib.media import archive, release
from lib.store import mirror
from lib.store.directory import Directory


def compiled(scratch, name, marker):
    (scratch / marker / f"{name}-{manager.WINDOWS}.exe").write_bytes(f"{name} {marker}".encode())


class Fixture(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())

    def test_the_fixture_builds_three_executables_and_installs_two(self):
        self.assertEqual(manager.declared(self.scratch), (["demo", "demo-ctl", "demo-api"], ["demo", "demo-ctl"]))
        self.assertIn("FIXTURE_VERSION", (self.scratch / "fixture.rs").read_text())

    def test_a_published_fixture_is_the_release_wharf_would_publish(self):
        listed = manager.declared(self.scratch)
        with mock.patch.object(manager, "compiled", side_effect=compiled):
            script = manager.published(self.scratch, "v1.0.0", listed)
        self.assertIn("$binaries = @('demo', 'demo-ctl')\n", script.read_text())
        self.assertTrue((script.parent / "canonical" / "manage.ps1").is_file())
        authority = self.scratch / "authority"
        seal = json.loads((authority / "v1/releases/stable/v1.0.0/seal.json").read_bytes())
        self.assertEqual(sorted(seal["artifacts"]), ["windows-x64"])
        archived = (authority / seal["artifacts"]["windows-x64"]["url"].removeprefix("https://releases.demo.perish.uk/")).read_bytes()
        self.assertEqual(zipfile.ZipFile(io.BytesIO(archived)).namelist(), ["demo.exe", "demo-ctl.exe"])


class Stepped(unittest.TestCase):
    def test_an_installed_state_holds_the_entries_and_one_owned_seat(self):
        wanted = manager.expected("v1.1.0", ["demo", "demo-ctl"])
        self.assertEqual(wanted["files"], ["bin/demo-ctl.exe", "bin/demo.exe", "root/.demo-manager", "root/v1.1.0/.demo-manager", "root/v1.1.0/demo-ctl.exe", "root/v1.1.0/demo.exe"])
        self.assertEqual(wanted["versions"], {"bin/demo.exe": "demo v1.1.0", "bin/demo-ctl.exe": "demo-ctl v1.1.0"})
        self.assertEqual(manager.expected("root", ["demo"])["files"], ["root/.demo-manager"])
        self.assertEqual(manager.expected("", ["demo"]), {"files": [], "versions": {}, "copies": {}})

    def test_the_rehearsal_installs_updates_and_uninstalls_through_both_managers(self):
        self.assertEqual([step[0] for step in manager.STEPS], ["install", "install", "update", "uninstall", "install", "uninstall"])
        self.assertEqual({step[1] for step in manager.STEPS}, {*manager.MARKERS, "canonical"})
        self.assertEqual(manager.STEPS[-1][3], "")

    def test_a_state_the_manager_did_not_leave_refuses_naming_the_step(self):
        with mock.patch.object(manager, "invoked"), mock.patch.object(manager, "observed", return_value=manager.expected("", [])):
            with self.assertRaisesRegex(Refusal, "after install through the v1.0.0 manager"):
                manager.stepped("pwsh", dict.fromkeys([*manager.MARKERS, "canonical"], Path("manage.ps1")), ["demo"], "http://127.0.0.1:1")

    def test_a_shell_the_machine_lacks_refuses(self):
        with mock.patch.object(manager.shutil, "which", return_value=None):
            with self.assertRaisesRegex(Refusal, "powershell is not on this machine"):
                manager.windows({"shell": "powershell"})


@unittest.skipIf(os.name == "nt", "Unix rendered manager")
class Unix(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name)
        self.bound = self.scratch / "bound"
        self.bound.mkdir()
        self.names = ["demo", "demo-ctl"]
        self.target = "x86_64-unknown-linux-gnu"
        self.marker = "v1.0.0"
        for name in self.names:
            path = self.bound / f"{name}-{self.target}"
            path.write_text(f'#!/bin/sh\ncase "$1" in\n--version) echo "{name} {self.marker}";;\n--refresh) echo supported;;\n*) exit 1;;\nesac\n')
            path.chmod(0o755)
        self.release = release.Release(manager.REPOSITORY, self.marker, manager.UNPINNED, manager.UNPINNED)
        self.managers = self.scratch / "managers"
        release.render(self.release, self.managers, {self.target: self.names})
        self.bucket = Directory(self.scratch / "authority")
        self.authority = release.place(self.release)[2]
        release.publish(self.release, release.Contents({self.target: self.bound}, self.managers, {self.target: self.names}, {}), self.bucket, lambda url: self.bucket.get(url.removeprefix(f"{self.authority}/")))
        self.server = mirror.served(self.scratch / "authority", self.authority)
        self.url = self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.root = self.scratch / "root"
        self.bin = self.scratch / "bin"
        self.script = self.managers / "manage.sh"
        self.done(self.call())

    def call(self):
        world = {"PATH": os.defpath, "HOME": str(self.scratch / "home")}
        return subprocess.run(["sh", str(self.script), "install", "--public-url", self.url, "--install-root", str(self.root), "--bin-dir", str(self.bin)], capture_output=True, text=True, env=world)

    def done(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def seated(self, name):
        return self.root / self.marker / name

    def corrupt(self, name):
        path = self.seated(name)
        body = f'#!/bin/sh\n[ "$1" = --version ] && echo "{name} {self.marker}" && exit 0\nexit 1\n'.encode()
        path.write_bytes(body)
        return body

    def artifact(self):
        seal = self.bucket.root / "v1/releases/stable/v1.0.0/seal.json"
        data = json.loads(seal.read_bytes())
        held = data["artifacts"]["linux-x64"]
        return seal, data, self.bucket.root / held["url"].removeprefix(f"{self.authority}/")

    def test_same_version_stale_command_is_repaired_and_digests_are_reported(self):
        stale = self.corrupt("demo")
        before = subprocess.run([str(self.bin / "demo"), "--refresh"], capture_output=True)
        self.assertNotEqual(before.returncode, 0)
        result = self.call()
        self.done(result)
        self.assertIn("installed sha256=" + hashlib.sha256(stale).hexdigest(), result.stdout)
        selected = (self.bound / f"demo-{self.target}").read_bytes()
        self.assertIn("selected sha256=" + hashlib.sha256(selected).hexdigest(), result.stdout)
        self.assertEqual(self.seated("demo").read_bytes(), selected)
        after = subprocess.run([str(self.bin / "demo"), "--refresh"], capture_output=True, text=True)
        self.assertEqual((after.returncode, after.stdout.strip()), (0, "supported"))
        self.assertFalse(any(self.root.glob("*/.*repair*")))

    def test_identical_reinstall_preserves_seated_file(self):
        before = self.seated("demo").stat().st_ino
        result = self.call()
        self.done(result)
        self.assertNotIn("repairing", result.stdout)
        self.assertEqual(before, self.seated("demo").stat().st_ino)

    def test_bad_archive_digest_preserves_all_installed_bytes(self):
        stale = self.corrupt("demo")
        _, _, path = self.artifact()
        path.write_bytes(path.read_bytes() + b"tampered")
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual(self.seated("demo").read_bytes(), stale)

    def test_verified_archive_with_wrong_version_preserves_installation(self):
        stale = self.corrupt("demo")
        bad = self.bound / f"demo-ctl-{self.target}"
        bad.write_text('#!/bin/sh\necho "demo-ctl v9.0.0"\n')
        body = archive.pack(archive.members(self.bound, self.target, self.names), "tar.gz")
        seal, data, path = self.artifact()
        path.write_bytes(body)
        data["artifacts"]["linux-x64"]["sha256"] = hashlib.sha256(body).hexdigest()
        seal.write_text(json.dumps(data, indent=2))
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("binary version mismatch", result.stderr)
        self.assertEqual(self.seated("demo").read_bytes(), stale)

    def test_unowned_entry_is_not_overwritten(self):
        entry = self.bin / "demo"
        entry.unlink()
        entry.write_bytes(b"unowned")
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unowned", result.stderr)
        self.assertEqual(entry.read_bytes(), b"unowned")

    def test_unowned_seat_is_not_repaired(self):
        stale = self.corrupt("demo")
        (self.root / self.marker / ".demo-manager").write_text("foreign\n")
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unowned", result.stderr)
        self.assertEqual(self.seated("demo").read_bytes(), stale)

    def test_redirected_secondary_binary_refuses_before_primary_repair(self):
        stale = self.corrupt("demo")
        outside = self.scratch / "outside"
        outside.write_bytes(b"protected")
        secondary = self.seated("demo-ctl")
        secondary.unlink()
        secondary.symlink_to(outside)
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("redirected", result.stderr)
        self.assertEqual(outside.read_bytes(), b"protected")
        self.assertEqual(self.seated("demo").read_bytes(), stale)

    def test_unexpected_content_refuses_before_repair(self):
        stale = self.corrupt("demo")
        extra = self.root / self.marker / ".valuable"
        extra.write_bytes(b"protected")
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected", result.stderr)
        self.assertEqual(extra.read_bytes(), b"protected")
        self.assertEqual(self.seated("demo").read_bytes(), stale)
