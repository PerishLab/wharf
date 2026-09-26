import unittest
from pathlib import Path

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

    def test_refuses_without_a_tracked_containerfile(self):
        repository = Repository(IMAGE)
        repository.git("rm", "-q", "Containerfile")
        repository.commit()
        with self.assertRaises(Refusal):
            oci.context(repository.root, repository.root / "missing", "demo")


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
