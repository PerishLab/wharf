import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.refusal import Refusal
from scripts import plan
from tests.lib.store.memory import Memory

LINUX = "x86_64-unknown-linux-gnu"
DARWIN = "aarch64-apple-darwin"


def product(body):
    root = Path(tempfile.mkdtemp())
    (root / "plumb.toml").write_text(body)
    return str(root)


class Placed(unittest.TestCase):
    SANTI = (
        '[release]\nproduct = "santi"\nbinaries = ["santi", "santi-api"]\n'
        f'targets = ["{LINUX}", "{DARWIN}"]\n'
        f'[release.binary.santi-api]\ntargets = ["{LINUX}"]\ninstall = false\n'
        '[release.deb]\nbinary = "santi-api"\nroot = "packaging/deb"\n'
    )

    def entries(self, body, release="run"):
        held = {"repository": "PerishLab/santi", "marker": "v0.1.0-rc.2", "commit": "a" * 40, "tree": "b" * 40, "source": product(body)}
        self.resolved = []
        entries = {}
        patches = (
            mock.patch.object(plan.basis, "resolve", side_effect=lambda source, names, target, runner: self.resolved.append((target, names)) or {"entry": "binary", "target": target}),
            mock.patch.object(plan.basis, "dependencies", side_effect=lambda source, names, target, runner: {"entry": "dependencies", "target": target, "binaries": names}),
            mock.patch.object(plan.package, "basis", side_effect=lambda source, declared, bound, version: {"entry": {"kind": "deb", "binary": bound, "executable": declared.binary, "version": version}}),
        )
        with patches[0], patches[1], patches[2]:
            plan.binaries(held, Memory(), entries)
            plan.placements(held, Memory(), entries)
        entries["release"] = {"decision": release}
        plan.validation(Memory(), entries)
        return entries

    def test_each_target_builds_every_executable_that_declares_it_in_one_entry(self):
        entries = self.entries(self.SANTI)
        self.assertEqual(self.resolved, [(LINUX, ["santi", "santi-api"]), (DARWIN, ["santi"])])
        self.assertEqual(sorted(name for name in entries if name.startswith("binary-")), ["binary-linux", "binary-macos"])

    def test_the_deb_is_built_from_the_bound_linux_executables_and_verified_after(self):
        entries = self.entries(self.SANTI)
        self.assertEqual((entries["deb"]["consumes"], entries["verify-deb"]["consumes"]), (["bind-linux"], ["deb"]))
        units = plan.units(entries)
        self.assertIn({"name": "[build] deb", "action": "deb", "target": "", "runner": plan.RUNNER, "prepare": []}, units["layer-3"])
        self.assertEqual(units["layer-4"], [{"name": "[verify] deb", "action": "deb-verify", "target": "", "runner": plan.RUNNER, "prepare": []}])

    def test_the_deb_version_is_the_debian_form_of_the_marker(self):
        held = {"repository": "PerishLab/santi", "marker": "v0.1.0-rc.2", "source": product(self.SANTI)}
        entries = {"bind-linux": {"key": "c" * 64}}
        with mock.patch.object(plan.release, "targets", return_value=[LINUX, DARWIN]), mock.patch.object(plan.package, "basis", return_value={"entry": "deb"}) as basis:
            plan.placements(held, Memory(), entries)
        self.assertEqual(basis.call_args.args[2:], ("c" * 64, "0.1.0~rc.2"))

    def test_nothing_is_packaged_when_the_release_is_already_published(self):
        entries = self.entries(self.SANTI, release="skip")
        self.assertEqual([entries[name]["decision"] for name in ("validate", "deb", "verify-deb")], ["skip", "skip", "skip"])

    def test_a_product_without_a_deb_plans_none(self):
        entries = self.entries(self.SANTI.split("[release.deb]")[0])
        self.assertNotIn("deb", entries)
        self.assertNotIn("verify-deb", entries)

    def test_a_placed_executable_must_be_built_for_linux(self):
        body = self.SANTI.replace(f'targets = ["{LINUX}"]\ninstall', f'targets = ["{DARWIN}"]\ninstall')
        with self.assertRaisesRegex(Refusal, f"santi-api is validated or placed on {LINUX}"):
            self.entries(body)

    def test_the_validated_executable_must_be_built_for_linux(self):
        body = f'[release]\nbinaries = ["santi", "santi-api"]\ntargets = ["{LINUX}", "{DARWIN}"]\n[release.binary.santi]\ntargets = ["{DARWIN}"]\n'
        with self.assertRaisesRegex(Refusal, "santi is validated or placed"):
            self.entries(body)

    def test_a_declared_target_no_executable_builds_refuses(self):
        body = f'[release]\nbinaries = ["santi"]\ntargets = ["{LINUX}", "{DARWIN}"]\n[release.binary.santi]\ntargets = ["{LINUX}"]\n'
        with self.assertRaisesRegex(Refusal, f"declares {DARWIN} and no executable"):
            self.entries(body)

    def test_an_image_naming_an_undeclared_executable_refuses(self):
        with self.assertRaisesRegex(Refusal, "santi-worker"):
            self.entries(self.SANTI + '[release.oci]\nbinary = "santi-worker"\n')
