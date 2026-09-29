import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from lib.media import chart, oci
from lib.refusal import Refusal
from tests.lib.media.repository import Repository


IMAGE = {"plumb.toml": '[release.oci]\nregistry = "ghcr.io"\nimage = "perishlab/demo"\naccount = "PerishLab"\n'}
CHART = {"plumb.toml": '[release.chart]\nregistry = "ghcr.io"\nchart = "perishlab/demo"\naccount = "PerishLab"\n'}


class Oci(unittest.TestCase):
    def test_reference_is_owner_scoped_and_lowercase(self):
        self.assertEqual(oci.reference("PerishLab/plumb", "1.2.3-beta.4"), "ghcr.io/perishlab/plumb:1.2.3-beta.4")

    def test_an_image_is_carried_only_where_plumb_declares_one(self):
        self.assertFalse(oci.carried(Repository().root))
        self.assertTrue(oci.carried(Repository(IMAGE).root))

    def test_context_holds_the_containerfile_and_named_binary(self):
        repository = Repository(IMAGE)
        binary = repository.root / "built"
        binary.write_bytes(b"elf")
        directory = oci.context(repository.root, binary, "demo")
        self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["Containerfile", "demo"])

    def test_source_context_holds_every_tracked_file_and_no_untracked_file(self):
        repository = Repository(IMAGE)
        repository.write("untracked", "outside\n")
        repository.write("Containerfile", "FROM changed\n")
        directory = oci.source_context(repository.root)
        self.assertTrue((directory / "Containerfile").is_file())
        self.assertEqual((directory / "Containerfile").read_text(), "FROM scratch\nCOPY demo /demo\n")
        self.assertTrue((directory / "packages/lib/package.json").is_file())
        self.assertFalse((directory / "untracked").exists())

    def test_refuses_without_a_tracked_containerfile(self):
        repository = Repository(IMAGE)
        repository.git("rm", "-q", "Containerfile")
        repository.commit()
        with self.assertRaises(Refusal):
            oci.context(repository.root, repository.root / "missing", "demo")


class Smoked(unittest.TestCase):
    def publish(self, answer):
        repository = Repository(IMAGE)
        binary = repository.root / "built"
        binary.write_bytes(b"elf")
        request = oci.Image(repository.root, binary, "demo", "ghcr.io/perishlab/demo:0.4.0-rc.1", "v0.4.0-rc.1")
        commands = []

        def runner(argv, cwd):
            commands.append(argv[:2])
            if argv[:2] == ["docker", "run"]:
                self.assertEqual(argv[2:], ["--rm", "--platform", "linux/amd64", request.reference, "--version"])
                return answer()
            return '["ghcr.io/perishlab/demo@sha256:0"]' if argv[:3] == ["docker", "image", "inspect"] else ""

        with mock.patch.object(oci, "exists", side_effect=[False, True]):
            try:
                return oci.publish(request, runner), commands
            except Refusal as refusal:
                return refusal, commands

    def test_pushes_only_after_the_image_reports_its_marker(self):
        result, commands = self.publish(lambda: "demo v0.4.0-rc.1\n")
        self.assertEqual(result["state"], "published")
        self.assertEqual(commands, [["docker", "build"], ["docker", "run"], ["docker", "push"], ["docker", "image"]])

    def test_refuses_an_image_whose_executable_does_not_start(self):
        def failed():
            raise subprocess.CalledProcessError(1, "docker", "", "demo: version `GLIBC_2.39' not found")

        result, commands = self.publish(failed)
        self.assertIsInstance(result, Refusal)
        self.assertIn("ghcr.io/perishlab/demo:0.4.0-rc.1 --version exited 1", str(result))
        self.assertIn("GLIBC_2.39", str(result))
        self.assertNotIn(["docker", "push"], commands)

    def test_refuses_an_image_reporting_another_line(self):
        result, commands = self.publish(lambda: "demo v0.3.0\n")
        self.assertIsInstance(result, Refusal)
        self.assertIn("ghcr.io/perishlab/demo:0.4.0-rc.1 --version reported 'demo v0.3.0'", str(result))
        self.assertIn("expected 'demo v0.4.0-rc.1'", str(result))
        self.assertNotIn(["docker", "push"], commands)


class PublishedSource(unittest.TestCase):
    def test_source_image_builds_without_running_an_executable_and_proves_its_public_digest(self):
        repository = Repository(IMAGE)
        request = oci.SourceImage(repository.root, "ghcr.io/perishlab/demo:0.4.0")
        commands = []

        def runner(argv, cwd):
            commands.append(argv[:2])
            if argv[:3] == ["docker", "image", "inspect"]:
                return '["ghcr.io/perishlab/demo@sha256:' + "0" * 64 + '"]'
            return ""

        with mock.patch.object(oci, "exists", return_value=False), mock.patch.object(oci, "public_digest", return_value="sha256:" + "0" * 64):
            result = oci.publish_source(request, runner)
        self.assertEqual(result["digests"], ["ghcr.io/perishlab/demo@sha256:" + "0" * 64])
        self.assertEqual(commands, [["docker", "build"], ["docker", "push"], ["docker", "image"]])

    def test_public_digest_uses_an_empty_docker_configuration(self):
        seen = {}

        def runner(argv, cwd):
            seen.update(argv=argv, cwd=cwd)
            return json.dumps("sha256:" + "1" * 64)

        self.assertEqual(oci.public_digest("ghcr.io/perishlab/demo:0.4.0", runner), "sha256:" + "1" * 64)
        self.assertEqual(seen["argv"][:4], ["docker", "--config", str(seen["cwd"]), "buildx"])

class Chart(unittest.TestCase):
    def test_lists_the_declared_chart_alone(self):
        self.assertEqual(chart.charts(Repository().root), [])
        extra = {"charts/other/Chart.yaml": "name: other\nversion: 0.0.0\n"}
        self.assertEqual(chart.charts(Repository(dict(CHART, **extra)).root), [{"name": "demo", "path": "charts/demo"}])
        self.assertEqual(chart.repository("PerishLab"), "oci://ghcr.io/perishlab/charts")

    def test_refuses_declared_chart_versions(self):
        with self.assertRaises(Refusal):
            chart.charts(Repository(dict(CHART, **{"charts/demo/Chart.yaml": "name: demo\nversion: 1.0.0\n"})).root)

    def test_refuses_a_declared_chart_the_tree_does_not_carry(self):
        with self.assertRaisesRegex(Refusal, "which no charts/\\*/Chart.yaml names"):
            chart.charts(Repository({"plumb.toml": CHART["plumb.toml"].replace("perishlab/demo", "perishlab/gone")}).root)
