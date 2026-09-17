"""The committed fixtures are the format's shared evidence; they must match the writer exactly."""

import json
import unittest
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship import identity

FIXTURES = Path(__file__).resolve().parents[2] / "spec" / "identity" / "fixtures"

BINDING = {
    "product": "demo-tool",
    "marker": "v1.2.3-beta.4",
    "digest": "2" * 64,
    "commit": "3" * 40,
    "workload": "4" * 64,
}


def region(prefix="DEMO_TOOL", commit="", target="x86_64-unknown-linux-gnu"):
    body = bytearray(identity.SIZE)
    body[:16] = identity.MAGIC
    body[16:16 + len(prefix)] = prefix.encode()
    body[80:80 + len(commit)] = commit.encode()
    body[120:120 + len(target)] = target.encode()
    return bytes(body)


def cases():
    unbound = region(commit="1" * 40)
    bound = identity.encode(unbound, BINDING)
    checksum = bytearray(bound)
    checksum[256] ^= 1
    payload = bytearray(bound)
    payload[identity.PAYLOAD + 3] ^= 1
    trailing = bytearray(bound)
    trailing[identity.SIZE - 1] = 1
    unbound_dirty = bytearray(unbound)
    unbound_dirty[300] = 1
    legacy = bytearray(unbound)
    legacy[:16] = b"PLUMB.IDENTITY.1"
    padding = bytearray(unbound)
    padding[70] = ord("X")
    return {
        "unbound.region": (unbound, {"origin": {"prefix": "DEMO_TOOL", "commit": "1" * 40, "target": "x86_64-unknown-linux-gnu"}, "binding": None}),
        "bound.region": (bound, {"origin": {"prefix": "DEMO_TOOL", "commit": "1" * 40, "target": "x86_64-unknown-linux-gnu"}, "binding": BINDING}),
        "refused-checksum.region": (bytes(checksum), {"refused": True}),
        "refused-payload.region": (bytes(payload), {"refused": True}),
        "refused-trailing-payload.region": (bytes(trailing), {"refused": True}),
        "refused-unbound-data.region": (bytes(unbound_dirty), {"refused": True}),
        "refused-legacy-magic.region": (bytes(legacy), {"refused": True}),
        "refused-noncanonical-padding.region": (bytes(padding), {"refused": True}),
    }


def write():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (body, expect) in cases().items():
        (FIXTURES / name).write_bytes(body)
        manifest[name] = expect
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


class Spec(unittest.TestCase):
    def test_fixtures_match_the_writer(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        expected = cases()
        self.assertEqual(sorted(manifest), sorted(expected))
        for name, (body, expect) in expected.items():
            with self.subTest(name):
                self.assertEqual((FIXTURES / name).read_bytes(), body)
                self.assertEqual(manifest[name], expect)

    def test_reader_outcomes(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        for name, expect in manifest.items():
            body = (FIXTURES / name).read_bytes()
            with self.subTest(name):
                if expect.get("refused"):
                    with self.assertRaises(Refusal):
                        identity.decode(body)
                else:
                    origin, binding = identity.decode(body)
                    self.assertEqual({"origin": origin, "binding": binding}, expect)


if __name__ == "__main__":
    write()
