from contextlib import contextmanager, ExitStack
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import plan
from tests.lib.store.memory import Memory


class Preparation(unittest.TestCase):
    def test_real_preparation_plan_contains_only_declared_build_chain_and_validation(self):
        for repository in ("PerishLab/demo", "PerishLab/plumb", "PerishLab/concord"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "plumb.toml").write_text('[release]\nbinaries=["demo"]\ntargets=["x86_64-unknown-linux-gnu","aarch64-apple-darwin","x86_64-pc-windows-msvc"]\n')
                held = {"repository": repository, "marker": "v1.2.3", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1", "source": directory, "steps": "{}", "preparing": True, "preparation": "{}"}
                identity = {name: held[name] for name in plan.IDENTITY}
                bucket = Memory()

                @contextmanager
                def prepared(request):
                    yield root, {"schema": "wharf.release.snapshot/v1", "source": identity, "head": "d" * 40, "tree": held["tree"], "packages": [], "controls": {}, "domain": {}, "guard": {}}, lambda: None

                def binary_basis(source, names, target, runner):
                    return {"entry": {"kind": "cargo-binary", "target": target, "binaries": names, "runner": runner}}

                with ExitStack() as stack:
                    for target, name, value in [(plan.snapshot, "prepared", prepared), (plan.toolchain, "versions", lambda: {}), (plan.r2, "configured", lambda: bucket), (plan.basis, "carried", lambda source: False), (plan.trial, "capture", lambda source, identity: None), (plan.node, "carried", lambda source: False), (plan.basis, "resolve", binary_basis), (plan.basis, "dependencies", binary_basis)]:
                        stack.enter_context(mock.patch.object(target, name, value))
                    captured = stack.enter_context(mock.patch.object(plan.material, "capture", return_value={"fixture": "actual dependency material"}))
                    admitted = stack.enter_context(mock.patch.object(plan.preflight, "planning"))
                    answered = stack.enter_context(mock.patch.object(plan.parameters, "answer"))
                    forbidden = [stack.enter_context(mock.patch.object(plan, name, side_effect=AssertionError("preparation reached " + name))) for name in ("media", "placements", "suites")]
                    stack.enter_context(mock.patch.object(plan.plan, "legacy", side_effect=AssertionError("preparation reached legacy publisher admission")))
                    result = plan.record(held)
                document = json.loads(bucket.get(result["plan"]))
                expected = {prefix + target["name"] for target in plan.BUILD["targets"] if target["target"] != "aarch64-unknown-linux-musl" for prefix in ("dependencies-", "binary-", "bind-", "smoke-")} | {"validate"}
                self.assertEqual(set(document["entries"]), expected)
                self.assertEqual(document["entries"]["validate"]["decision"], "run")
                self.assertEqual(admitted.call_count, 2)
                self.assertFalse(bucket.exists(plan.plan.publication(held)))
                self.assertEqual(document["context"]["binary_declaration"]["installed"], ["demo"])
                layers = plan.units(document["entries"])
                self.assertTrue(all(unit["action"] in ("dependencies", "binary", "bind", "smoke", "validate") for layer in layers.values() for unit in layer))
                self.assertEqual(captured.call_count, int(repository != "PerishLab/demo"))
                self.assertEqual(document["context"].get("material"), None if repository == "PerishLab/demo" else {"fixture": "actual dependency material"})
