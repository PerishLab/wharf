from contextlib import contextmanager
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical
from scripts import plan
from tests.lib.store.memory import Memory


class Frozen(unittest.TestCase):
    def test_plan_persists_binary_selection_from_the_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            manifest = source / "plumb.toml"
            manifest.write_text('[release]\nbinaries=["demo","demo-api"]\ntargets=["x86_64-unknown-linux-gnu"]\n[release.binary.demo-api]\ninstall=false\n')
            held = {"repository": "PerishLab/demo", "marker": "v1.2.3-rc.1", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1", "source": directory, "steps": json.dumps({name: {"outputs": {"decision": "skip", "presence": "present" if name == "release" else "none"}} for name in plan.MEDIA})}
            identity = {name: held[name] for name in plan.IDENTITY}
            bucket = Memory()

            @contextmanager
            def prepared(request):
                yield source, {"schema": "wharf.release.snapshot/v1", "source": identity, "head": "d" * 40, "tree": held["tree"], "packages": [], "controls": {}, "domain": {}, "guard": {}}, lambda: None

            with mock.patch.object(plan.snapshot, "prepared", prepared), mock.patch.object(plan.toolchain, "versions", return_value={}), mock.patch.object(plan.r2, "configured", return_value=bucket), mock.patch.object(plan.basis, "carried", return_value=False), mock.patch.object(plan.trial, "capture", return_value=None), mock.patch.object(plan, "suites"), mock.patch.object(plan, "binaries"), mock.patch.object(plan, "placements"), mock.patch.object(plan.node, "carried", return_value=False), mock.patch.object(plan.parameters, "answer") as answered:
                result = plan.record(held)
            frozen = json.loads(bucket.get(result["plan"]))["context"]["binary_declaration"]
            self.assertEqual(frozen["built"], ["demo", "demo-api"])
            self.assertEqual(frozen["installed"], ["demo"])
            self.assertEqual(frozen["manifest"], plan.inventory.sha(manifest.read_bytes()))
            self.assertIn(mock.call({"binaryproof": "true"}), answered.call_args_list)
            manifest.write_text("changed after planning")
            self.assertEqual(json.loads(bucket.get(result["plan"]))["context"]["binary_declaration"], frozen)
