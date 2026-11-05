import os
import tempfile
import unittest
from pathlib import Path

from lib.package.openwrt import package
from lib.package.openwrt.version import version
from lib.refusal import Refusal
from tests.lib.media.repository import Repository

DECLARATION = '[release]\nbinaries=["dynet"]\ntargets=["aarch64-unknown-linux-musl"]\n[release.apk]\nbinary="dynet"\nroot="packaging/apk"\nprofile="openwrt-aarch64-cortex-a53"\n'
FILES = {"plumb.toml": DECLARATION, "packaging/apk/package.toml": 'description="Gateway"\nlicense="MIT"\ndepends=["libc"]\n', "packaging/apk/root/etc/dynet/example.conf": "example\n"}


class Declaration(unittest.TestCase):
    def test_versions_preserve_prerelease_order_without_claiming_a_new_revision(self):
        self.assertEqual([version(value) for value in ("v1.2.3-alpha.1", "v1.2.3-beta.2", "v1.2.3-rc.3", "v1.2.3")], ["1.2.3_alpha1-r0", "1.2.3_beta2-r0", "1.2.3_rc3-r0", "1.2.3-r0"])
        with self.assertRaises(Refusal):
            version("v1.2.3-rc.0")

    def test_declarations_are_closed_to_the_openwrt_profile_and_relative_root(self):
        for before, after in (("packaging/apk", "../apk"), ("packaging/apk", "/etc"), ("packaging/apk", ""), ("openwrt-aarch64-cortex-a53", "alpine-aarch64")):
            root = Repository(dict(FILES, **{"plumb.toml": DECLARATION.replace(before, after)})).root
            with self.subTest(after=after), self.assertRaises(Refusal):
                package.declared(root, "dynet")

    def test_the_basis_binds_the_binary_recipe_tool_world_and_tracked_payload(self):
        root = Repository(FILES).root
        held = package.declared(root, "dynet")
        basis = package.basis(root, held, "a" * 64, "1.2.3-r0")
        self.assertEqual(basis["entry"]["binary"], "a" * 64)
        self.assertEqual(basis["entry"]["architecture"], "aarch64_cortex-a53")
        self.assertIn("@sha256:", basis["builder"])
        self.assertEqual((basis["compat"], basis["compression"], basis["epoch"]), ("3.0.5", "deflate:9", 0))
        self.assertEqual(set(basis["root"]["files"]), {"package.toml", "root/etc/dynet/example.conf"})

    def test_scripts_links_and_automatic_enablement_refuse(self):
        for path in ("post-install", "root/etc/rc.d/S99dynet"):
            root = Repository(dict(FILES, **{f"packaging/apk/{path}": "forbidden"})).root
            with self.subTest(path=path), self.assertRaises(Refusal):
                package.listing(root, "packaging/apk")
        repository = Repository(FILES)
        os.symlink("package.toml", repository.root / "packaging/apk/link")
        repository.commit()
        with self.assertRaises(Refusal):
            package.listing(repository.root, "packaging/apk")


class Payload(unittest.TestCase):
    def test_a_payload_cannot_replace_the_bound_executable(self):
        root = Repository(dict(FILES, **{"packaging/apk/root/usr/bin/dynet": "foreign"})).root
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch)
            binary = path / "bound"
            binary.write_bytes(b"bound")
            with self.assertRaisesRegex(Refusal, "twice"):
                package.staged(package.Package(root, package.Declared("dynet", "packaging/apk"), binary, "1.2.3-r0", path / "out"), path / "stage")

    def test_package_tools_have_no_network_credentials_or_source_write_mount(self):
        argv = package.container(package.BUILDER, [(Path("."), "/input", True)])
        self.assertIn("none", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertTrue(argv[argv.index("--mount") + 1].endswith(",readonly"))
        self.assertNotIn("--privileged", argv)
