import hashlib
import io
import json
import os
import subprocess
import tarfile
import unittest

from lib.media import release
from tests.scripts.manager.test_manager import Harness


@unittest.skipIf(os.name == "nt", "Unix rendered manager")
class Inspection(Harness):
    def snapshot(self):
        held = {}
        for root in (self.root, self.bin):
            for path in (root, *root.rglob("*")):
                stat = path.lstat()
                body = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
                held[str(path)] = (stat.st_ino, stat.st_mode, stat.st_mtime_ns, body)
        return held

    def inspect(self, version=None, script=None):
        arguments = ["sh", str(script or self.script), "inspect", "--channel", "stable", "--public-url", self.url, "--install-root", str(self.root), "--bin-dir", str(self.bin)]
        if version:
            arguments.extend(["--version", version])
        before = self.snapshot()
        result = subprocess.run(arguments, capture_output=True, text=True, env={"PATH": os.defpath, "HOME": str(self.scratch / "home")})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def publish(self, version, commit):
        bound = self.scratch / version
        bound.mkdir()
        for name in self.names:
            path = bound / f"{name}-{self.target}"
            path.write_text(f'#!/bin/sh\necho "{name} {version}"\n')
            path.chmod(0o755)
        selected = release.Release(self.release.repository, version, commit, self.release.wharf)
        managers = self.scratch / (version + "-managers")
        release.render(selected, managers, {self.target: self.names})
        release.publish(selected, release.Contents({self.target: bound}, managers, {self.target: self.names}, {}), self.bucket, lambda url: self.bucket.get(url.removeprefix(f"{self.authority}/")))
        return selected, managers

    def test_matching_bytes_have_sealed_identity(self):
        code, report = self.inspect()
        self.assertEqual(code, 0)
        self.assertTrue(report["verified"] and report["converged"])
        self.assertEqual(report["target"], "linux-x64")
        for row in report["files"]:
            self.assertEqual(row["state"], "matching")
            self.assertEqual(row["marker"], self.marker)
            self.assertEqual(row["sha256"], hashlib.sha256(self.seated(row["name"]).read_bytes()).hexdigest())
            self.assertEqual(row["archive_sha256"], report["archive_sha256"])

    def test_replacement_cannot_prove_identity_by_printing_a_version(self):
        sentinel = self.scratch / "executed"
        self.seated("demo").write_text(f'#!/bin/sh\ntouch "{sentinel}"\necho "demo {self.marker}"\n')
        code, report = self.inspect()
        self.assertEqual(code, 1)
        row = report["files"][0]
        self.assertEqual(row["state"], "replaced")
        self.assertIsNone(row["marker"])
        self.assertIsNone(row["commit"])
        self.assertEqual(row["recovery_argv"][-4:], ["--install-root", str(self.root), "--bin-dir", str(self.bin)])
        self.assertFalse(sentinel.exists())

    def test_older_sealed_bytes_are_stale(self):
        self.publish("v1.1.0", "1" * 40)
        code, report = self.inspect("v1.1.0")
        self.assertEqual(code, 1)
        self.assertEqual({row["state"] for row in report["files"]}, {"stale"})
        self.assertEqual({row["marker"] for row in report["files"]}, {self.marker})
        self.assertEqual(report["commit"], "1" * 40)

    def test_directory_names_are_only_lookup_hints(self):
        moved = self.root / "v9.9.9"
        (self.root / self.marker).rename(moved)
        for name in self.names:
            (self.bin / name).unlink()
            (self.bin / name).symlink_to(moved / name)
        code, report = self.inspect()
        self.assertEqual(code, 0)
        self.assertEqual(report["files"][0]["marker"], self.marker)
        self.assertEqual(report["files"][0]["lookup_hint"], "v9.9.9")
        self.publish("v1.1.0", "1" * 40)
        code, report = self.inspect("v1.1.0")
        self.assertEqual(code, 2)
        self.assertEqual(report["files"][0]["state"], "unknown")
        self.assertIsNone(report["files"][0]["marker"])

    def test_missing_entry_is_reported_without_repair(self):
        self.seated("demo").unlink()
        code, report = self.inspect()
        self.assertEqual(code, 1)
        self.assertEqual(report["files"][0]["state"], "missing")
        self.assertIsNone(report["files"][0]["sha256"])
        self.assertTrue((self.bin / "demo").is_symlink())

    def test_tampered_archive_is_unknown(self):
        _, _, archived = self.artifact()
        archived.write_bytes(archived.read_bytes() + b"tampered")
        code, report = self.inspect()
        self.assertEqual(code, 2)
        self.assertFalse(report["verified"])
        self.assertEqual({row["state"] for row in report["files"]}, {"unknown"})

    def test_unavailable_seal_is_unknown(self):
        seal, _, _ = self.artifact()
        seal.unlink()
        code, report = self.inspect()
        self.assertEqual(code, 2)
        self.assertIsNone(report["archive_sha256"])

    def test_malformed_seal_metadata_is_unknown(self):
        seal, data, _ = self.artifact()
        for field, value in (("product", "foreign"), ("commit", "invalid"), ("schema", 2), ("channel", "rc")):
            with self.subTest(field=field):
                changed = dict(data)
                changed[field] = value
                seal.write_text(json.dumps(changed, indent=2))
                code, report = self.inspect()
                self.assertEqual(code, 2)
                self.assertFalse(report["verified"])

    def test_archive_links_are_refused_before_extraction(self):
        seal, data, _ = self.artifact()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as packed:
            entry = tarfile.TarInfo("demo")
            entry.type = tarfile.SYMTYPE
            entry.linkname = str(self.scratch / "outside")
            packed.addfile(entry)
        body = output.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        key = f"v1/objects/sha256/{digest}/demo-{self.target}.tar.gz"
        self.bucket.create(key, body)
        data["artifacts"]["linux-x64"].update(sha256=digest, url=f"{self.authority}/{key}")
        seal.write_text(json.dumps(data, indent=2))
        code, report = self.inspect()
        self.assertEqual(code, 2)
        self.assertFalse(report["verified"])
        self.assertFalse((self.scratch / "outside").exists())

    def test_candidate_promotion_requires_the_same_sealed_commit(self):
        candidate, managers = self.publish("v1.1.0-rc.1", "1" * 40)
        self.root = self.scratch / "candidate-root"
        self.bin = self.scratch / "candidate-bin"
        self.script = managers / "manage.sh"
        self.marker = candidate.marker
        self.done(self.call())
        self.publish("v1.1.0", "1" * 40)
        code, report = self.inspect("v1.1.0")
        self.assertEqual(code, 1)
        self.assertFalse(report["converged"])
        self.assertEqual({row["state"] for row in report["files"]}, {"candidate"})
        self.assertEqual(report["files"][0]["marker"], candidate.marker)
        self.assertEqual(report["files"][0]["recovery_argv"], ["sh", "manage.sh", "install", "--channel", "stable"])
        self.publish("v1.2.0", "2" * 40)
        code, report = self.inspect("v1.2.0")
        self.assertEqual(code, 1)
        self.assertEqual({row["state"] for row in report["files"]}, {"stale"})

    def test_moving_stable_is_bound_to_pointer_seal_digest(self):
        release.point(self.release, self.managers, self.bucket)
        pointer = self.bucket.root / "v1/channels/stable.json"
        data = json.loads(pointer.read_bytes())
        seal, _, _ = self.artifact()
        data["seal"]["sha256"] = hashlib.sha256(seal.read_bytes().replace(self.authority.encode(), self.url.encode())).hexdigest()
        pointer.write_text(json.dumps(data, indent=2))
        script = self.scratch / "canonical.sh"
        body = (self.managers / "canonical/manage.sh").read_text()
        script.write_text(body.replace(self.authority, self.url))
        code, report = self.inspect(script=script)
        self.assertEqual(code, 0)
        self.assertEqual(report["marker"], self.marker)
        pointer = self.bucket.root / "v1/channels/stable.json"
        data = json.loads(pointer.read_bytes())
        data["seal"]["sha256"] = "0" * 64
        pointer.write_text(json.dumps(data, indent=2))
        code, report = self.inspect(script=script)
        self.assertEqual(code, 2)
        self.assertFalse(report["verified"])
