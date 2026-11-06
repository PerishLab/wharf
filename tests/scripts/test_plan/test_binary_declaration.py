from contextlib import contextmanager
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind import control, inventory
from lib.process.association import chain, producer, public
from tests.association.world import World
from lib.refusal import Refusal
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
            native = json.loads(bucket.get(result["plan"]))["context"]["native_declaration"]
            self.assertEqual(native["source"], identity)
            self.assertEqual(native["model"]["targets"][frozen["target"]], {name: frozen[name] for name in ("built", "installed")})
            manifest.write_text("changed after planning")
            self.assertEqual(json.loads(bucket.get(result["plan"]))["context"]["binary_declaration"], frozen)
            self.assertEqual(json.loads(bucket.get(result["plan"]))["context"]["native_declaration"], native)

    def test_full_declaration_exposes_only_bounded_binary_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plumb.toml"
            path.write_text('[release]\nbinaries=["demo","demo-api"]\ntargets=["x86_64-unknown-linux-gnu","aarch64-apple-darwin","x86_64-pc-windows-msvc"]\n[release.binary.demo-api]\ninstall=false\ntargets=["x86_64-pc-windows-msvc"]\n[release.windows]\nscope="machine"\n[private]\nendpoint="https://internal.invalid"\n')
            identity = {"repository": "PerishLab/demo", "marker": "v1.2.3", "commit": "a" * 40, "tree": "b" * 40}
            captured = plan.inventory.native(directory, identity)
            full = plan.inventory.frozen(captured, identity)
            self.assertEqual(full["manifest"], plan.inventory.sha(path.read_bytes()))
            self.assertEqual(full["windows"], "machine")
            self.assertEqual(full["targets"]["x86_64-pc-windows-msvc"], {"built": ["demo", "demo-api"], "installed": ["demo"]})
            self.assertEqual(full["targets"]["aarch64-apple-darwin"], {"built": ["demo"], "installed": ["demo"]})
            self.assertNotIn("internal.invalid", json.dumps(captured))
            self.assertNotIn("private", json.dumps(captured))

    def test_source_capture_refuses_duplicate_manifest_fields_and_special_files(self):
        with self.assertRaises(Refusal):
            plan.inventory.model(b'[release]\nbinaries=["demo"]\nbinaries=["other"]\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"repository": "PerishLab/demo", "marker": "v1.2.3", "commit": "a" * 40, "tree": "b" * 40}
            path = root / "plumb.toml"
            path.symlink_to(root / "other.toml")
            (root / "other.toml").write_text('[release]\nbinaries=["demo"]\n')
            with self.assertRaises(Refusal):
                plan.inventory.native(root, identity)
            path.unlink()
            path.write_bytes(b"x" * 1048577)
            with self.assertRaises(Refusal):
                plan.inventory.native(root, identity)

    def test_capture_refuses_noncanonical_source_before_reading_material(self):
        identity = {"repository": "PerishLab/demo", "marker": "v1.2.3", "commit": "a" * 40, "tree": "b" * 40}
        for field in identity:
            changed = dict(identity, **{field: None})
            with self.subTest(field=field), mock.patch.object(plan.inventory, "Path", side_effect=AssertionError("material read")), self.assertRaises(Refusal):
                plan.inventory.native("unused", changed)


class Declaration(unittest.TestCase):
    def test_private_proof_and_public_readback_need_no_anonymous_source(self):
        world = World()
        source_url = f"https://raw.githubusercontent.com/PerishLab/demo/{world.source['commit']}/plumb.toml"
        del world.documents[source_url]
        with world.wired(), mock.patch.object(world, "fetch", wraps=world.fetch) as fetched:
            result = producer.produce(world.held)
            with mock.patch.object(producer.r2, "configured", side_effect=AssertionError("private workload read")), mock.patch.object(producer.r2, "writer", side_effect=AssertionError("release writer")):
                readback = public.check(world.held)
        self.assertEqual(result["state"], "proven")
        self.assertTrue(result["proofs"])
        self.assertEqual(set(readback["targets"]), set(world.native))
        self.assertEqual(readback["schema"], "wharf.binary.native.readback/v3")
        self.assertNotIn(source_url, [call.args[0] for call in fetched.call_args_list])

    def test_missing_native_capture_refuses_without_guessing_or_source_fetch(self):
        world = World()
        del world.plan["context"]["native_declaration"]
        with self.assertRaisesRegex(Refusal, "frozen native declaration"):
            chain.frozen(world.plan, world.source)

    def test_foreign_source_or_manifest_refuses_the_frozen_model(self):
        world = World()
        for field in ("repository", "marker", "commit", "tree"):
            document = copy.deepcopy(world.plan)
            document["context"]["native_declaration"]["source"][field] = "foreign"
            with self.subTest(field=field), self.assertRaisesRegex(Refusal, "another source"):
                chain.frozen(document, world.source)
        document = copy.deepcopy(world.plan)
        document["context"]["native_declaration"]["model"]["manifest"] = "0" * 64
        with self.assertRaisesRegex(Refusal, "differs from the frozen declaration"):
            chain.frozen(document, world.source)

    def test_structured_native_model_rejects_tampered_and_unbounded_inventories(self):
        world = World()
        native_target = "aarch64-apple-darwin"
        changes = [{"extra": True}, {"manifest": "invalid"}, {"windows": "global"}, {"targets": {"foreign": {"built": [], "installed": []}}}]
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                inventory.complete(dict(world.model, **changed))
        for held in ({"built": ["demo", "demo"], "installed": ["demo"]}, {"built": ["demo"], "installed": ["other"]}, {"built": ["demo"], "installed": ["../demo"]}, {"built": ["x" * 257], "installed": []}, {"built": ["demo"], "installed": [], "extra": True}):
            value = copy.deepcopy(world.model)
            value["targets"][native_target] = held
            with self.subTest(held=held), self.assertRaises(Refusal):
                inventory.complete(value)

    def test_source_headers_keep_exact_string_types_without_coercion(self):
        world = World()
        for field in ("commit", "tree"):
            source = dict(world.source, **{field: "1" * 40})
            self.assertEqual(control.source(source), source)
            source[field] = int(source[field])
            with self.subTest(field=field), self.assertRaisesRegex(Refusal, "must be strings"):
                control.source(source)
            with self.subTest(frozen=field), self.assertRaisesRegex(Refusal, "must be strings"):
                inventory.frozen(world.plan["context"]["native_declaration"], source)
        for field in world.source:
            with self.subTest(field=field), self.assertRaises(Refusal):
                control.source(dict(world.source, **{field: None}))
