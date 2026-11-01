import io
import tarfile
import unittest

from lib.media import archive as packed
from lib.process.association import archive
from lib.refusal import Refusal
from tests.association.fixtures import MACOS, WINDOWS


class Archive(unittest.TestCase):
    def test_exact_installation_files_and_representation(self):
        for selected, fmt, name in ((MACOS, "tar.gz", "demo"), (WINDOWS, "zip", "demo.exe")):
            body = packed.pack([(name, b"binary")], fmt)
            with self.subTest(target=selected):
                self.assertEqual(archive.unpack(body, ["demo"], selected), {name: b"binary"})
                with self.assertRaises(Refusal):
                    archive.unpack(body, ["other"], selected)
                with self.assertRaises(Refusal):
                    archive.unpack(body + b"trailing", ["demo"], selected)

    def test_extra_missing_duplicate_or_escaping_files_refuse(self):
        for names in (["demo", "extra"], ["other"], ["demo", "demo"], ["../demo"]):
            body = packed.tarball([(name, b"binary") for name in names])
            with self.subTest(names=names), self.assertRaises(Refusal):
                archive.unpack(body, ["demo"], MACOS)
        body = packed.zipball([("demo.exe", b"binary"), ("extra", b"extra")])
        with self.assertRaises(Refusal):
            archive.unpack(body, ["demo"], WINDOWS)

    def test_tar_links_and_noncanonical_permissions_refuse(self):
        for mode in (0o644, 0o755):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as held:
                entry = tarfile.TarInfo("demo")
                entry.type = tarfile.SYMTYPE if mode == 0o755 else tarfile.REGTYPE
                entry.mode = mode
                held.addfile(entry)
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                archive.unpack(buffer.getvalue(), ["demo"], MACOS)
