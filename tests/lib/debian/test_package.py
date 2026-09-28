import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.debian import package
from lib.process import git
from lib.refusal import Refusal
from tests.lib.media.repository import Repository

CONTROL = "Package: santi-api\nVersion: __VERSION__\nMaintainer: PerishLab <ops@perish.uk>\nDepends: adduser\nDescription: santi server\n"
UNIT = "[Unit]\nDescription=santi\n\n[Service]\nExecStart=/usr/bin/santi-api\n"
ROOT = {
    "packaging/deb/control": CONTROL,
    "packaging/deb/postinst": "#!/bin/sh\nset -e\n",
    "packaging/deb/root/lib/systemd/system/santi.service": UNIT,
    "packaging/deb/root/etc/santi/santi.env.example": "SANTI_LISTEN=127.0.0.1:8080\n",
    "packaging/deb/root/usr/share/santi/README": "santi\n",
}
DPKG = shutil.which("dpkg-deb")


class Declared(unittest.TestCase):
    def test_the_root_is_a_plain_relative_directory(self):
        for root in ("/etc", "../outside", "", "a\\\\b"):
            with self.subTest(root), self.assertRaisesRegex(Refusal, "root must name"):
                package.declared(Repository({"plumb.toml": f'[release.deb]\nroot = "{root}"\n'}).root, "santi-api")
        self.assertEqual(package.declared(Repository({"plumb.toml": '[release.deb]\nroot = "packaging/deb/"\n'}).root, "santi-api"), package.Declared("santi-api", "packaging/deb"))


class Listed(unittest.TestCase):
    def test_the_listing_is_what_git_tracks_under_the_root(self):
        held = package.listing(Repository(ROOT).root, "packaging/deb")
        self.assertEqual(sorted(held), ["control", "postinst", "root/etc/santi/santi.env.example", "root/lib/systemd/system/santi.service", "root/usr/share/santi/README"])
        self.assertTrue(all(mode == "100644" for mode, _ in held.values()))

    def test_a_link_in_the_root_refuses(self):
        repository = Repository(ROOT)
        os.symlink("santi.service", repository.root / "packaging/deb/root/lib/systemd/system/alias.service")
        repository.commit()
        with self.assertRaisesRegex(Refusal, "refuses links"):
            package.listing(repository.root, "packaging/deb")

    def test_an_unknown_entry_or_a_missing_control_refuses(self):
        with self.assertRaisesRegex(Refusal, "alone, not conffiles"):
            package.listing(Repository(dict(ROOT, **{"packaging/deb/conffiles": "/etc/x\n"})).root, "packaging/deb")
        with self.assertRaisesRegex(Refusal, "carries its control"):
            package.listing(Repository({"packaging/deb/root/etc/x": "x\n"}).root, "packaging/deb")

    def test_units_and_depends_are_read_from_the_source(self):
        root = Repository(ROOT).root
        held = package.Declared("santi-api", "packaging/deb")
        self.assertEqual(list(package.units(root, held)), ["/lib/systemd/system/santi.service"])
        self.assertEqual(package.depends(root, held), ["adduser"])

    def test_the_basis_names_the_bound_executable_the_root_and_the_packer(self):
        root = Repository(ROOT).root
        with mock.patch.object(package, "dpkg", return_value="dpkg-deb 1.22.6"):
            held = package.basis(root, package.Declared("santi-api", "packaging/deb"), "c" * 64, "0.1.0~rc.2")
        self.assertEqual(held["entry"], {"kind": "deb", "binary": "c" * 64, "executable": "santi-api", "version": "0.1.0~rc.2", "architecture": "amd64"})
        self.assertEqual((held["dpkg"], held["epoch"], held["root"]["path"]), ("dpkg-deb 1.22.6", 0, "packaging/deb"))


@unittest.skipUnless(DPKG, "dpkg-deb builds the package")
class Built(unittest.TestCase):
    def setUp(self):
        self.repository = Repository(ROOT)
        self.binary = Path(tempfile.mkdtemp()) / "santi-api-x86_64-unknown-linux-gnu"
        self.binary.write_bytes(b"#!/bin/sh\necho 'santi-api v0.1.0-rc.2'\n")

    def build(self, root=None):
        output = Path(tempfile.mkdtemp()) / "deb"
        held = package.Package(root or self.repository.root, package.Declared("santi-api", "packaging/deb"), self.binary, "0.1.0~rc.2", output)
        return package.build(held), output

    def members(self, path):
        listed = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(path)], check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(listed)) as held:
            return {member.name.removeprefix("./"): member for member in held.getmembers()}

    def field(self, path, name):
        return subprocess.run(["dpkg-deb", "--field", str(path), name], check=True, capture_output=True, text=True).stdout.strip()

    def test_building_twice_gives_the_same_bytes(self):
        first, one = self.build()
        clone = Path(tempfile.mkdtemp()) / "clone"
        git(self.repository.root, "clone", "-q", ".", str(clone))
        second, two = self.build(clone)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((one / "santi-api_0.1.0~rc.2_amd64.deb").read_bytes(), (two / "santi-api_0.1.0~rc.2_amd64.deb").read_bytes())

    def test_the_package_carries_the_executable_the_payload_and_wharf_s_control(self):
        receipt, output = self.build()
        deb = output / receipt["file"]
        self.assertEqual(receipt["file"], "santi-api_0.1.0~rc.2_amd64.deb")
        self.assertEqual([self.field(deb, name) for name in ("Package", "Version", "Architecture")], ["santi-api", "0.1.0~rc.2", "amd64"])
        members = self.members(deb)
        binary = members["usr/bin/santi-api"]
        self.assertEqual((binary.mode, binary.uid, binary.gid, binary.uname, binary.mtime), (0o755, 0, 0, "root", 0))
        self.assertEqual(members["lib/systemd/system/santi.service"].mode, 0o644)
        self.assertEqual(members["etc/santi"].mode, 0o755)
        self.assertEqual(receipt["conffiles"], ["/etc/santi/santi.env.example"])
        control = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)], check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(control)) as held:
            self.assertEqual(held.extractfile("./conffiles").read(), b"/etc/santi/santi.env.example\n")
            self.assertEqual(held.getmember("./postinst").mode, 0o755)

    def test_a_payload_that_places_the_executable_itself_refuses(self):
        self.repository.write("packaging/deb/root/usr/bin/santi-api", "shadow\n")
        self.repository.commit()
        with self.assertRaisesRegex(Refusal, "placed twice"):
            self.build()

    def test_a_payload_that_writes_the_control_area_refuses(self):
        self.repository.write("packaging/deb/root/DEBIAN/postinst", "#!/bin/sh\n")
        self.repository.commit()
        with self.assertRaisesRegex(Refusal, "DEBIAN"):
            self.build()


class Mocked(unittest.TestCase):
    def test_the_packer_runs_pinned_to_one_instant_and_one_thread(self):
        calls = []

        def runner(argv, cwd, env=None):
            calls.append((argv, env))
            if argv[:2] == ["dpkg-deb", "--build"] or "--build" in argv:
                Path(argv[-1]).write_bytes(b"deb")
                return ""
            return "Debian 'dpkg-deb' package archive backend version 1.22.6 (amd64).\n"

        binary = Path(tempfile.mkdtemp()) / "santi-api"
        binary.write_bytes(b"elf")
        output = Path(tempfile.mkdtemp()) / "deb"
        package.build(package.Package(Repository(ROOT).root, package.Declared("santi-api", "packaging/deb"), binary, "0.1.0", output), runner)
        argv, env = next((argv, env) for argv, env in calls if "--build" in argv)
        self.assertEqual(argv[:5], ["dpkg-deb", "--root-owner-group", "--threads-max=1", "-Zxz", "--build"])
        self.assertEqual((env["SOURCE_DATE_EPOCH"], env["TZ"], env["LC_ALL"]), ("0", "UTC", "C"))
