import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lib import parameters
from lib.refusal import Refusal
from lib.store import plan, workload
from scripts import media
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/ensign", "marker": "v0.4.0", "wharf": "c" * 40, "run": "7", "attempt": "1"}
LINUX = "x86_64-unknown-linux-gnu"


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in media.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())

    def test_refusal_exits_two(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(media.main(["oci-publish", "--source", "missing"]), 2)
        self.assertIn("media: refused", error.getvalue())


class Imaged(unittest.TestCase):
    def publish(self, body):
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text(body)
        bucket = Memory()
        bound = Path(tempfile.mkdtemp()) / "bound"
        bound.mkdir()
        for name in ("ensign", "ensign-api"):
            (bound / f"{name}-{LINUX}").write_bytes(name.encode())
        workload.publish(bucket, "b" * 64, workload.Produced(bound, {}, {}))
        plan.record(bucket, dict(CONTEXT, commit="a" * 40, tree="d" * 40), {"bind-linux": {"key": "b" * 64, "decision": "run"}, "oci": {"decision": "run"}}, {})
        seen = {}

        def pushed(image):
            seen.update(binary=Path(image.binary).read_bytes(), name=image.name, reference=image.reference)
            return {"state": "published"}

        with mock.patch.object(media.r2, "configured", return_value=bucket), mock.patch.object(media.oci, "publish", side_effect=pushed):
            media.oci_publish(dict(CONTEXT, planned="1", source=str(source)))
        return seen

    def test_the_image_carries_the_executable_it_names(self):
        seen = self.publish(f'[release]\nbinaries = ["ensign", "ensign-api"]\ntargets = ["{LINUX}"]\n[release.oci]\nbinary = "ensign-api"\n')
        self.assertEqual((seen["name"], seen["binary"]), ("ensign-api", b"ensign-api"))
        self.assertEqual(seen["reference"], "ghcr.io/perishlab/ensign:0.4.0")

    def test_an_image_naming_none_carries_the_primary(self):
        seen = self.publish(f'[release]\nbinaries = ["ensign-api", "ensign"]\ntargets = ["{LINUX}"]\n[release.oci]\n')
        self.assertEqual((seen["name"], seen["binary"]), ("ensign", b"ensign"))

    def test_an_image_whose_executable_is_not_built_for_linux_refuses(self):
        body = f'[release]\nbinaries = ["ensign", "ensign-api"]\ntargets = ["{LINUX}", "aarch64-apple-darwin"]\n[release.binary.ensign-api]\ntargets = ["aarch64-apple-darwin"]\n[release.oci]\nbinary = "ensign-api"\n'
        with self.assertRaisesRegex(Refusal, "not built for"):
            self.publish(body)
