import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.identity import guard
from lib.media import release
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

LINUX = "x86_64-unknown-linux-gnu"
COMMIT = "a" * 40
DEPOT = "0123456789abcdef" * 4
UNGUARDED = "0a39d831b34519176930506d747992a0d1f16fe2749db51a91f42edeaf9a9a9a"


def bound(name):
    root = Path(tempfile.mkdtemp())
    held = {}
    for target in release.LAYOUT["targets"]:
        directory = root / target
        directory.mkdir()
        suffix = ".exe" if "windows" in target else ""
        (directory / f"{name}-{target}{suffix}").write_bytes(f"binary {target}".encode())
        held[target] = directory
    return held


def managers():
    root = Path(tempfile.mkdtemp())
    (root / "manage.sh").write_text("sh")
    (root / "manage.ps1").write_text("ps1")
    return root


def answering(document, calls=None):
    def runner(argv, cwd, env=None):
        if calls is not None:
            calls.append((argv, env))
        return json.dumps(document) if isinstance(document, dict) else document
    return runner


def missing(argv, cwd, env=None):
    raise subprocess.CalledProcessError(2, argv, "", "error: unrecognized subcommand 'authority'")


def plumb(marker="v0.57.0"):
    return release.Release("PerishLab/plumb", marker, COMMIT, "b" * 40)


class Reported(unittest.TestCase):
    def test_reads_the_authority_the_bound_linux_binary_reports(self):
        calls = []
        held = guard.reported(plumb(), bound("plumb"), answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}, calls))
        self.assertEqual(held, {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT})
        argv, env = calls[0]
        self.assertTrue(argv[0].endswith(f"/{LINUX}/plumb-{LINUX}"))
        self.assertEqual(argv[1:], ["release", "authority", "--json"])
        self.assertEqual(set(env), {"HOME", "PATH"})

    def test_a_prerelease_reports_its_own_marker(self):
        held = guard.reported(plumb("v0.57.0-rc.1"), bound("plumb"), answering({"producer": f"v0.57.0-rc.1@{COMMIT}", "depot": DEPOT}))
        self.assertEqual(held["producer"], f"v0.57.0-rc.1@{COMMIT}")

    def test_refuses_an_authority_that_does_not_match_the_release(self):
        cases = {
            "exactly": {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT, "extra": "x"},
            "as strings": {"producer": f"v0.57.0@{COMMIT}"},
            "producer version": {"producer": f"v0.56.9@{COMMIT}", "depot": DEPOT},
            "producer commit": {"producer": f"v0.57.0@{'c' * 40}", "depot": DEPOT},
            "producer commit ''": {"producer": "v0.57.0", "depot": DEPOT},
            "lowercase hex": {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT.upper()},
        }
        for reason, document in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(Refusal, reason):
                guard.reported(plumb(), bound("plumb"), answering(document))
        with self.assertRaisesRegex(Refusal, "did not print JSON"):
            guard.reported(plumb(), bound("plumb"), answering("plumb v0.57.0"))

    def test_refuses_a_binary_that_lacks_the_command(self):
        with self.assertRaisesRegex(Refusal, "release authority --json exited 2; a guard-authority release needs a binary that reports it"):
            guard.reported(plumb(), bound("plumb"), missing)

    def test_refuses_a_release_without_its_linux_binary(self):
        held = bound("plumb")
        del held[LINUX]
        with self.assertRaisesRegex(Refusal, LINUX):
            guard.reported(plumb(), held, answering({}))

    def test_another_product_runs_nothing_and_reports_nothing(self):
        santi = release.Release("PerishLab/santi", "v1.2.3", COMMIT, "b" * 40)
        self.assertIsNone(guard.reported(santi, bound("santi"), missing))
        self.assertEqual(guard.GUARD["repositories"], ["PerishLab/plumb"])


class Sealed(unittest.TestCase):
    def publish(self, published, contents, bucket):
        with mock.patch.object(release, "generator", lambda held: {"version": "wharf fixed"}):
            release.publish(published, contents, bucket, lambda url: bucket.get(url.split(".perish.uk/", 1)[1]))
        return bucket.get(release.sealed(published))

    def test_another_products_seal_keeps_its_bytes(self):
        santi = release.Release("PerishLab/santi", "v1.2.3-rc.1", COMMIT, "b" * 40)
        held = bound("santi")
        contents = release.Contents(held, managers(), {target: ["santi"] for target in held}, {}, guard.reported(santi, held, missing))
        body = self.publish(santi, contents, Memory())
        self.assertNotIn(b'"guard"', body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), UNGUARDED)

    def test_plumbs_seal_carries_the_guard_and_is_read_back(self):
        held = bound("plumb")
        authority = guard.reported(plumb(), held, answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}))
        contents = release.Contents(held, managers(), {target: ["plumb"] for target in held}, {}, authority)
        bucket = Memory()
        seal = json.loads(self.publish(plumb(), contents, bucket))
        self.assertEqual(seal["guard"], {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT})
        self.assertEqual(seal["releaseVersion"], seal["guard"]["producer"].split("@")[0])
        self.assertEqual(seal["commit"], seal["guard"]["producer"].split("@")[1])
        other = release.Contents(held, contents.managers, contents.installed, {}, {**authority, "depot": "f" * 64})
        with self.assertRaisesRegex(Refusal, "immutable"):
            self.publish(plumb(), other, bucket)

    def test_a_seal_that_is_not_served_as_written_refuses(self):
        held = bound("plumb")
        authority = {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}
        contents = release.Contents(held, managers(), {target: ["plumb"] for target in held}, {}, authority)
        with self.assertRaisesRegex(Refusal, "does not serve the written seal"):
            release.publish(plumb(), contents, Memory(), lambda url: b"{}")


if __name__ == "__main__":
    unittest.main()
