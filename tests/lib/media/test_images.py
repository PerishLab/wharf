import unittest
from pathlib import Path

from lib.media import chart, oci
from lib.refusal import Refusal
from tests.lib.media.repository import Repository


class Oci(unittest.TestCase):
    def test_reference_is_owner_scoped_and_lowercase(self):
        self.assertEqual(oci.reference("PerishLab/plumb", "1.2.3-beta.4"), "ghcr.io/perishlab/plumb:1.2.3-beta.4")

    def test_context_holds_the_containerfile_and_named_binary(self):
        repository = Repository()
        binary = repository.root / "built"
        binary.write_bytes(b"elf")
        directory = oci.context(repository.root, binary, "demo")
        self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["Containerfile", "demo"])

    def test_refuses_without_a_tracked_containerfile(self):
        repository = Repository()
        repository.git("rm", "-q", "Containerfile")
        repository.commit()
        with self.assertRaises(Refusal):
            oci.context(repository.root, repository.root / "missing", "demo")


class Chart(unittest.TestCase):
    def test_lists_unversioned_charts(self):
        self.assertEqual(chart.charts(Repository().root), [{"name": "demo", "path": "charts/demo"}])
        self.assertEqual(chart.repository("PerishLab"), "oci://ghcr.io/perishlab/charts")

    def test_refuses_declared_chart_versions(self):
        with self.assertRaises(Refusal):
            chart.charts(Repository({"charts/demo/Chart.yaml": "name: demo\nversion: 1.0.0\n"}).root)
