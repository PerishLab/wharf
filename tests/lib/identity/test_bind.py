import struct
import tempfile
import unittest
from pathlib import Path

from lib.identity import bind, elf, format, region
from lib.refusal import Refusal


def region(prefix="PLUMB", target="x86_64-unknown-linux-gnu"):
    bytes_ = bytearray(format.SIZE)
    bytes_[:16] = format.MAGIC
    bytes_[16:16 + len(prefix)] = prefix.encode()
    bytes_[120:120 + len(target)] = target.encode()
    return bytes(bytes_)


def executable(content, sections=(".relid",), relocate=False):
    names = b"\0" + b"".join(name.encode() + b"\0" for name in sections) + b".shstrtab\0" + b".rela\0"
    body = bytearray(64)
    offsets = []
    for _ in sections:
        offsets.append(len(body))
        body += content
    strings = len(body)
    body += names
    shoff = len(body)
    headers = [struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    cursor = 1
    for index, name in enumerate(sections):
        headers.append(struct.pack("<IIQQQQIIQQ", cursor, 1, 0, 0, offsets[index], len(content), 0, 0, 1, 0))
        cursor += len(name) + 1
    shstrndx = len(headers)
    headers.append(struct.pack("<IIQQQQIIQQ", cursor, 3, 0, 0, strings, len(names), 0, 0, 1, 0))
    cursor += len(".shstrtab") + 1
    if relocate:
        headers.append(struct.pack("<IIQQQQIIQQ", cursor, 4, 0, 0, 0, 0, 0, 1, 8, 24))
    body += b"".join(headers)
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 16, 2)
    struct.pack_into("<Q", header, 40, shoff)
    struct.pack_into("<HHH", header, 58, 64, len(headers), shstrndx)
    body[:64] = header
    return bytes(body)


BINDING = {
    "product": "plumb",
    "marker": "v0.38.0-beta.2",
    "digest": "2" * 64,
    "commit": "3" * 40,
    "workload": "4" * 64,
}


class Identity(unittest.TestCase):
    def test_binds_and_reads_back(self):
        image = executable(region())
        self.assertEqual(bind.inspect(image), ({"prefix": "PLUMB", "commit": "", "target": "x86_64-unknown-linux-gnu"}, None))
        bound = bind.bind(image, BINDING)
        self.assertEqual(len(bound), len(image))
        self.assertEqual(bind.inspect(bound)[1], BINDING)

    def test_rebinding_the_same_identity_is_idempotent(self):
        bound = bind.bind(executable(region()), BINDING)
        self.assertEqual(bind.bind(bound, BINDING), bound)

    def test_refuses_a_different_identity_on_a_bound_image(self):
        bound = bind.bind(executable(region()), BINDING)
        with self.assertRaises(Refusal):
            bind.bind(bound, dict(BINDING, marker="v0.38.0"))

    def test_refuses_a_foreign_product(self):
        with self.assertRaises(Refusal):
            bind.bind(executable(region()), dict(BINDING, product="concord"))

    def test_refuses_malformed_fields(self):
        for field, value in (("marker", "latest"), ("commit", "3" * 39), ("workload", "G" * 64)):
            with self.subTest(field), self.assertRaises(Refusal):
                bind.bind(executable(region()), dict(BINDING, **{field: value}))

    def test_refuses_tampered_payload(self):
        image = bytearray(bind.bind(executable(region()), BINDING))
        start, _ = elf.locate(bytes(image))
        image[start + format.PAYLOAD + 5] ^= 1
        with self.assertRaises(Refusal):
            bind.inspect(bytes(image))

    def test_refuses_missing_duplicate_or_relocated_regions(self):
        for image in (executable(region(), sections=(".data",)), executable(region(), sections=(".relid", ".relid")), executable(region(), relocate=True)):
            with self.subTest(), self.assertRaises(Refusal):
                bind.inspect(image)

    def test_refuses_non_elf(self):
        with self.assertRaises(Refusal):
            bind.inspect(b"MZ" + bytes(100))

    def test_digest_is_stable(self):
        release = bind.Release("PerishLab/plumb", "v1.0.0", "a" * 40, "b" * 40)
        self.assertEqual(bind.digest(release), bind.digest(bind.Release(*vars(release).values())))


class Performed(unittest.TestCase):
    TARGET = "x86_64-unknown-linux-gnu"

    def built(self, names, prefix="SANTI"):
        directory = Path(tempfile.mkdtemp()) / "built"
        directory.mkdir()
        for name in names:
            (directory / f"{name}-{self.TARGET}").write_bytes(executable(region(prefix, self.TARGET)))
        (directory / "receipt.json").write_text("{}")
        return directory

    def test_every_executable_built_for_the_target_is_found(self):
        self.assertEqual(bind.executables(self.built(["santi-api", "santi"]), self.TARGET), ["santi", "santi-api"])
        with self.assertRaisesRegex(Refusal, "no executable built for"):
            bind.executables(self.built(["santi"]), "aarch64-apple-darwin")

    def test_every_executable_is_bound_to_the_product_identity(self):
        directory = self.built(["santi", "santi-api"])
        output = Path(tempfile.mkdtemp()) / "bound"
        release = bind.Release("PerishLab/santi", "v0.1.0-rc.1", "a" * 40, "b" * 40)
        receipt = bind.perform(bind.Artifact(directory, "santi", self.TARGET), release, "4" * 64, str(output))
        self.assertEqual([held["file"] for held in receipt["binaries"]], [f"santi-{self.TARGET}", f"santi-api-{self.TARGET}"])
        for name in ("santi", "santi-api"):
            self.assertEqual(bind.inspect((output / f"{name}-{self.TARGET}").read_bytes())[1]["product"], "santi")
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["receipt.json", f"santi-api-{self.TARGET}", f"santi-{self.TARGET}"])

    def test_an_executable_compiled_under_another_prefix_refuses(self):
        directory = self.built(["santi-api"], prefix="SANTI_API")
        release = bind.Release("PerishLab/santi", "v0.1.0", "a" * 40, "b" * 40)
        with self.assertRaises(Refusal):
            bind.perform(bind.Artifact(directory, "santi", self.TARGET), release, "4" * 64, str(Path(tempfile.mkdtemp()) / "bound"))
