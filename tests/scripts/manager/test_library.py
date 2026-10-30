import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.identity.guard import library
from lib.refusal import Refusal

EXPECTED = {"repository": "PerishLab/demo", "marker": "v1.0.0-rc.1", "commit": "a" * 40, "tree": "b" * 40}
DOMAIN = {"rust.version": "1.96.1", "node.version": "24.18.0", "pnpm.version": "11.28.5"}
PASSING = "test result: ok. 59 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"


def blob(path="demo-1.0.0-rc.1/Cargo.lock"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(path)
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    return gzip.compress(buffer.getvalue())


class Published(unittest.TestCase):
    def test_distribution_must_be_complete_and_bind_every_source_field(self):
        complete = {"schema": 1, "state": "complete", "media": {"cargo": "published"}, **EXPECTED}
        for changed in ({"state": "incomplete"}, {"commit": "c" * 40}, {"tree": "c" * 40}, {"schema": 2}):
            with mock.patch.object(library.registry, "fetch_bytes", return_value=json.dumps({**complete, **changed}).encode()), self.assertRaises(Refusal):
                library.ready(EXPECTED)
        with mock.patch.object(library.registry, "fetch_bytes", return_value=json.dumps(complete).encode()):
            self.assertEqual(len(library.ready(EXPECTED)), 64)

    def test_package_checksum_and_embedded_release_identity_both_bind(self):
        archive = blob()
        line = {"cksum": hashlib.sha256(archive).hexdigest(), "yanked": False}
        manifest = {"package": {"name": "demo", "version": "1.0.0-rc.1", "metadata": {"perish": {"release": {"schema": 1, **EXPECTED}}}}}
        with mock.patch.object(library.registry, "declared", return_value="https://cargo.perish.uk/"), mock.patch.object(library.registry, "record", return_value=line), mock.patch.object(library.registry, "download", return_value="crate"), mock.patch.object(library.registry, "fetch_bytes", return_value=archive), mock.patch.object(library.index, "manifest", return_value=manifest):
            found, receipt = library.packaged(".", EXPECTED, ("demo", "perish"))
            self.assertEqual(found, archive)
            self.assertEqual(receipt["sha256"], line["cksum"])
            line["cksum"] = "0" * 64
            with self.assertRaisesRegex(Refusal, "checksum"):
                library.packaged(".", EXPECTED, ("demo", "perish"))
            line["cksum"] = hashlib.sha256(archive).hexdigest()
            manifest["package"]["metadata"]["perish"]["release"]["commit"] = "c" * 40
            with self.assertRaisesRegex(Refusal, "metadata"):
                library.packaged(".", EXPECTED, ("demo", "perish"))

    def test_source_rejects_stable_before_checkout_or_network(self):
        with self.assertRaisesRegex(Refusal, "exact RC"):
            library.identity({"source": ".", "marker": "v1.0.0"})

    def test_archive_rejects_files_outside_its_package(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(Refusal):
            library.unpack(blob("outside"), Path(root), {"package": "demo", "version": "1.0.0-rc.1"})


class Execution(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "Cargo.lock").write_text("locked")

    def test_fixed_all_features_command_records_real_totals_and_lock(self):
        with mock.patch.object(library.suite, "attempt", return_value=(0, PASSING)) as attempt:
            result = library.execute(self.root, {})
        self.assertEqual(result["totals"], {"passed": 59, "failed": 0, "ignored": 0})
        self.assertEqual(attempt.call_args.args[0], ["cargo", "test", "--locked", "--all-features", "--no-fail-fast"])

    def test_failures_empty_results_and_ignored_tests_refuse(self):
        for code, log in ((101, PASSING), (0, ""), (0, PASSING.replace("0 ignored", "1 ignored")), (0, PASSING.replace("0 failed", "1 failed"))):
            with mock.patch.object(library.suite, "attempt", return_value=(code, log)), self.assertRaises(Refusal):
                library.execute(self.root, {})

    def test_changed_packaged_lock_refuses(self):
        def changed(argv, cwd, env):
            (cwd / "Cargo.lock").write_text("changed")
            return 0, PASSING
        with mock.patch.object(library.suite, "attempt", side_effect=changed), self.assertRaisesRegex(Refusal, "packaged lock"):
            library.execute(self.root, {})

    def test_execution_environment_excludes_credentials_and_ambient_cargo(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "private", "WHARF_APP_PRIVATE_KEY": "private", "CARGO_REGISTRIES_PERISH_TOKEN": "private", "RUSTFLAGS": "foreign"}, clear=True):
            env = library.environment(self.root, "1.96.1")
        self.assertFalse(any("TOKEN" in name or "PRIVATE" in name for name in env))
        self.assertNotIn("RUSTFLAGS", env)
        self.assertEqual(env["CARGO_HOME"], str(self.root / "cargo"))


class Native(unittest.TestCase):
    def test_a_successful_native_run_rechecks_publication_and_cleans_its_seat(self):
        held = {"source": ".", "domain": json.dumps(DOMAIN)}
        observed = []
        def execute(root, env):
            observed.append(root)
            self.assertTrue(root.exists())
            return {"totals": {"passed": 59}}
        with mock.patch.object(library, "identity", return_value=EXPECTED), mock.patch.object(library, "ready", return_value="d" * 64) as ready, mock.patch.object(library.publish, "publishable", return_value=[("demo", "perish")]), mock.patch.object(library, "packaged", return_value=(blob(), {"package": "demo", "version": "1.0.0-rc.1"})), mock.patch.object(library, "run", return_value=f"rustc 1.96.1 (fixture)\nhost: {library.TARGET}"), mock.patch.object(library, "execute", side_effect=execute):
            result = library.case(held)
        self.assertTrue(result["cleanup"])
        self.assertFalse(observed[0].exists())
        self.assertEqual(ready.call_count, 2)

    def test_cross_compilation_does_not_count_as_native(self):
        with mock.patch.object(library, "identity", return_value=EXPECTED), mock.patch.object(library, "ready", return_value="d" * 64), mock.patch.object(library, "run", return_value="rustc 1.96.1 (fixture)\nhost: x86_64-unknown-linux-gnu"), self.assertRaisesRegex(Refusal, "native Windows"):
            library.case({"source": ".", "domain": json.dumps(DOMAIN)})
