import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.package.openwrt import signing, verify
from lib.refusal import Refusal


class Signing(unittest.TestCase):
    def test_publication_refuses_without_its_explicit_signing_seat(self):
        with mock.patch.dict(signing.os.environ, {}, clear=True), self.assertRaisesRegex(Refusal, "WHARF_APK_SIGNING_KEY"):
            signing.placed(Path("absent"), "https://example.invalid", Path("absent-output"))

    def test_signing_must_preserve_all_metadata_and_payload_identities(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "input.apk"
            source.write_bytes(b"unsigned")
            calls = []

            def runner(argv, cwd):
                calls.append(argv)
                if "adbdump" in argv:
                    return "metadata\n" if sum("adbdump" in call for call in calls) == 1 else "changed\n# sig ignored\n"
                return ""

            with self.assertRaisesRegex(Refusal, "changed package metadata"):
                signing.signed(source, root / "private.pem", root / "output", runner)
            self.assertTrue(any("verify" in call for call in calls))
            self.assertFalse(any("--allow-untrusted" in call for call in calls if "verify" in call))

    def test_a_signer_success_without_a_trusted_result_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "input.apk"
            source.write_bytes(b"unsigned")

            def runner(argv, cwd):
                if "verify" in argv:
                    raise subprocess.CalledProcessError(1, argv)
                return "metadata\n"

            with self.assertRaises(subprocess.CalledProcessError):
                signing.signed(source, root / "private.pem", root / "output", runner)


class Installation(unittest.TestCase):
    def test_wrong_installed_package_version_stops_execution_and_cleans_the_container(self):
        calls = []

        def runner(argv, cwd):
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                return "container"
            if "info" in argv:
                return "dynet-0.0.1-r0\n"
            return ""

        check = verify.Check(Path("candidate.apk"), "dynet", "v1.2.3", "a" * 64, "1.2.3-r0")
        with self.assertRaisesRegex(Refusal, "different package version"):
            verify.installed(check, Path("/tmp/signed/candidate.apk"), runner)
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "container"])
        self.assertFalse(any("/usr/bin/dynet" in call for call in calls))
        self.assertFalse(any("--allow-untrusted" in call for call in calls))

    def test_a_different_installed_binary_never_reaches_the_version_probe(self):
        calls = []

        def runner(argv, cwd):
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                return "container"
            if "info" in argv:
                return "dynet-1.2.3-r0\n"
            if "sha256sum" in argv:
                return "wrong /usr/bin/dynet\n"
            return ""

        check = verify.Check(Path("candidate.apk"), "dynet", "v1.2.3", "a" * 64, "1.2.3-r0")
        with self.assertRaisesRegex(Refusal, "differs from the verified"):
            verify.installed(check, Path("/tmp/signed/candidate.apk"), runner)
        self.assertEqual(calls[-1], ["docker", "rm", "-f", "container"])
        self.assertFalse(any("--version" in call for call in calls))
