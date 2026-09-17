import unittest

from lib import resources
from lib.identity import format, region
from lib.refusal import Refusal

BINDING = {
    "product": "demo-tool",
    "marker": "v1.2.3-beta.4",
    "digest": "2" * 64,
    "commit": "3" * 40,
    "workload": "4" * 64,
}
ORIGIN = {"prefix": "DEMO_TOOL", "commit": "1" * 40, "target": "x86_64-unknown-linux-gnu"}


def unbound():
    body = bytearray(format.SIZE)
    for name, value in (("magic", format.MAGIC), ("prefix", b"DEMO_TOOL"), ("commit", b"1" * 40), ("target", b"x86_64-unknown-linux-gnu")):
        start = format.LAYOUT[name][0]
        body[start:start + len(value)] = value
    return bytes(body)


def flipped(body, offset, value=None):
    changed = bytearray(body)
    changed[offset] = changed[offset] ^ 1 if value is None else value
    return bytes(changed)


def cases():
    empty = unbound()
    bound = region.encode(empty, BINDING)
    legacy = bytearray(empty)
    legacy[:16] = b"PLUMB.IDENTITY.1"
    return {
        "unbound.region": (empty, {"origin": ORIGIN, "binding": None}),
        "bound.region": (bound, {"origin": ORIGIN, "binding": BINDING}),
        "refused-checksum.region": (flipped(bound, 256), {"refused": True}),
        "refused-payload.region": (flipped(bound, format.PAYLOAD + 3), {"refused": True}),
        "refused-trailing-payload.region": (flipped(bound, format.SIZE - 1, 1), {"refused": True}),
        "refused-unbound-data.region": (flipped(empty, 300, 1), {"refused": True}),
        "refused-legacy-magic.region": (bytes(legacy), {"refused": True}),
        "refused-noncanonical-padding.region": (flipped(empty, 70, ord("X")), {"refused": True}),
    }


class Fixtures(unittest.TestCase):
    def test_fixtures_match_the_writer(self):
        manifest = resources.read_json("identity/fixtures/manifest.json")
        expected = cases()
        self.assertEqual(sorted(manifest), sorted(expected))
        for name, (body, expect) in expected.items():
            with self.subTest(name):
                self.assertEqual(resources.read_bytes(f"identity/fixtures/{name}"), body)
                self.assertEqual(manifest[name], expect)

    def test_reader_outcomes(self):
        for name, expect in resources.read_json("identity/fixtures/manifest.json").items():
            body = resources.read_bytes(f"identity/fixtures/{name}")
            with self.subTest(name):
                if expect.get("refused"):
                    self.assertRaises(Refusal, region.decode, body)
                else:
                    origin, binding = region.decode(body)
                    self.assertEqual({"origin": origin, "binding": binding}, expect)
