import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.debian import verify
from lib.refusal import Refusal

UNIT = "/lib/systemd/system/santi.service"
DIGEST = hashlib.sha256(b"unit").hexdigest()
IMAGE = verify.VERIFIER["image"]


class Docker:
    def __init__(self, **answers):
        self.calls = []
        self.answers = {
            "status": "Package: santi-api\nStatus: install ok installed\nVersion: 0.1.0~rc.2\nDescription: santi\n server\n",
            "listed": f"/.\n/usr\n/usr/bin\n/usr/bin/santi-api\n{UNIT}\n",
            "sums": f"{DIGEST}  {UNIT}\n",
            "version": "santi-api v0.1.0-rc.2\n",
            **answers,
        }

    def __call__(self, argv, cwd, env=None):
        self.calls.append(argv)
        spoken = argv[argv.index("container") + 1:] if "container" in argv else argv
        if argv[:3] == ["docker", "run", "-d"]:
            return "container\n"
        found = {("dpkg", "-s"): "status", ("dpkg", "-L"): "listed", ("sha256sum",): "sums", ("/usr/bin/santi-api", "--version"): "version"}
        for prefix, name in found.items():
            if tuple(spoken[:len(prefix)]) == prefix:
                if isinstance(self.answers[name], Exception):
                    raise self.answers[name]
                return self.answers[name]
        return ""

    def inside(self):
        return [argv[argv.index("container") + 1:] for argv in self.calls if argv[:2] == ["docker", "exec"]]


class Verified(unittest.TestCase):
    def setUp(self):
        self.deb = Path(tempfile.mkdtemp()) / "santi-api_0.1.0~rc.2_amd64.deb"
        self.deb.write_bytes(b"deb")
        self.output = Path(tempfile.mkdtemp()) / "verified"

    def check(self, depends=("adduser",), units=None):
        return verify.Check(self.deb, "santi-api", "0.1.0~rc.2", "v0.1.0-rc.2", depends, {UNIT: DIGEST} if units is None else units)

    def verify(self, docker, check=None):
        return verify.verify(check or self.check(), str(self.output), verify.Tools(run=docker, stream=docker))

    def test_the_package_is_installed_strictly_in_the_pinned_image_after_its_depends(self):
        docker = Docker()
        receipt = self.verify(docker)
        self.assertEqual(docker.calls[0], ["docker", "pull", "--platform", "linux/amd64", IMAGE])
        self.assertEqual(docker.calls[1], ["docker", "run", "-d", "--platform", "linux/amd64", IMAGE, "sleep", "infinity"])
        self.assertIn("@sha256:", IMAGE)
        self.assertTrue(IMAGE.startswith("docker.io/library/ubuntu:24.04@sha256:"))
        self.assertEqual(docker.calls[2], ["docker", "cp", str(self.deb), "container:/tmp/santi-api_0.1.0~rc.2_amd64.deb"])
        self.assertEqual(docker.inside()[:3], [["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends", "adduser"], ["dpkg", "-i", "/tmp/santi-api_0.1.0~rc.2_amd64.deb"]])
        self.assertIn(["/usr/bin/santi-api", "--version"], docker.inside())
        self.assertNotIn("systemctl", json.dumps(docker.calls))
        self.assertEqual(docker.calls[-1], ["docker", "rm", "-f", "container"])
        self.assertEqual((receipt["version"], receipt["reported"]), ("0.1.0~rc.2", "santi-api v0.1.0-rc.2"))
        self.assertEqual(json.loads((self.output / "receipt.json").read_text()), receipt)

    def test_a_package_with_no_depends_asks_apt_for_nothing(self):
        docker = Docker()
        self.verify(docker, self.check(depends=()))
        self.assertEqual(docker.inside()[0], ["dpkg", "-i", "/tmp/santi-api_0.1.0~rc.2_amd64.deb"])

    def test_every_mismatch_refuses_and_the_container_is_removed(self):
        cases = {
            "status": ("Package: santi-api\nStatus: install ok unpacked\nVersion: 0.1.0~rc.2\n", "not an installed package"),
            "listed": ("/usr/bin/santi-api\n", "does not list /lib/systemd/system/santi.service"),
            "sums": (f"{'0' * 64}  {UNIT}\n", "differs from the product's source"),
            "version": ("santi v0.1.0-rc.2\n", "expected 'santi-api v0.1.0-rc.2'"),
        }
        for name, (answer, message) in cases.items():
            with self.subTest(name):
                docker = Docker(**{name: answer})
                with self.assertRaisesRegex(Refusal, message):
                    verify.verify(self.check(), str(Path(tempfile.mkdtemp()) / "out"), verify.Tools(run=docker, stream=docker))
                self.assertEqual(docker.calls[-1], ["docker", "rm", "-f", "container"])

    def test_a_version_other_than_the_release_refuses(self):
        docker = Docker(status="Package: santi-api\nStatus: install ok installed\nVersion: 0.1.0~rc.1\n")
        with self.assertRaisesRegex(Refusal, "version 0.1.0~rc.1"):
            self.verify(docker)

    def test_a_failing_install_refuses_with_what_it_said(self):
        def docker(argv, cwd, env=None):
            if "dpkg" in argv and "-i" in argv:
                raise subprocess.CalledProcessError(1, argv, output="dependency problems")
            return "container\n"
        with self.assertRaisesRegex(Refusal, "dependency problems"):
            self.verify(docker, self.check(depends=()))
