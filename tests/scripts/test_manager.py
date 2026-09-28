import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lib.refusal import Refusal
from scripts import manager


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
