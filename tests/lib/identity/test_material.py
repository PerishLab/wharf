import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content.static import evidence
from lib.identity.guard import execution, material, mismatch, snapshot
from lib.process import git
from lib.refusal import Refusal
from tests.lib.identity.test_guard import AUTHORITY, runtime
from tests.lib.media.repository import Repository

DOMAIN = {"rust.version": "1.96.1", "node.version": "24.18.0", "pnpm.version": "11.28.5"}


class Material(unittest.TestCase):
    def setUp(self):
        self.repository = Repository({"Cargo.lock": "original\n"})
        self.addCleanup(shutil.rmtree, self.repository.root)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        source = self.repository.root
        identity = {"repository": "PerishLab/example", "marker": "v1.0.0-rc.1", "commit": git(source, "rev-parse", "HEAD"), "tree": git(source, "rev-parse", "HEAD^{tree}")}
        git(source, "tag", "original", identity["commit"])
        env = snapshot.environment(self.directory, DOMAIN)
        held = {"root": source, "tools": {"git": {"path": shutil.which("git")}}, "env": env, "inspect": evidence.inspect}
        (source / "Cargo.lock").write_bytes(b"current\r\n")
        snapshot.git(held, "add", "--update")
        tree = snapshot.git(held, "write-tree")
        head = snapshot.committed(held, {"tree": tree})
        self.controls = {"plumb": "plumb v0.87.0", "ectropy": "ectropy v0.20.0", "authority": AUTHORITY, "stable": {}}
        self.public = {}
        for name in ("plumb", "ectropy"):
            version = self.controls[name].split()[1]
            authority = mismatch.RELEASES["authority"].format(name=name)
            seal = {"product": name, "releaseVersion": version, "commit": "2" * 40}
            if name == "plumb":
                seal["guard"] = AUTHORITY
            body = json.dumps(seal).encode()
            location = f"{authority}/v1/releases/stable/{version}/seal.json"
            self.public[location] = body
            self.public[f"{authority}/v1/channels/stable.json"] = json.dumps(dict(seal, schema=1, channel="stable", seal={"url": location, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})).encode()
            self.controls["stable"][name] = {"releaseVersion": version, "commit": seal["commit"]}
        resolution = {"context": "ci-latest", "tree": tree, "packages": []}
        guard = json.loads(runtime(commit=head, tree=tree, root="/verified/original", proof={"resolution": resolution, "platform": "linux-x86_64"}))
        self.expected = dict(schema="wharf.release.snapshot/v1", source=identity, head=head, tree=tree, packages=[], controls=self.controls, domain=DOMAIN, guard=guard)
        self.payload = material.capture(source, self.expected)
        self.request = snapshot.Request(source, identity, DOMAIN, self.expected, self.directory / "consumed")

    def consume(self, payload=None):
        return material.prepared(self.request, self.payload if payload is None else payload, False)

    def patches(self):
        return mock.patch.multiple(material.platform, machine=lambda: "aarch64"), mock.patch.object(material.release, "fetch", side_effect=lambda url: self.public[url])

    def test_exact_bytes_tree_head_and_original_guard_are_consumed_without_control_execution(self):
        machine, public = self.patches()
        calls = []
        def inspect(argv, cwd, env):
            calls.append(Path(argv[0]).name)
            return evidence.inspect(argv, cwd, env)
        with machine, public, material.prepared(self.request, self.payload, False, inspect) as (root, record, confirm):
            self.assertEqual((root / "Cargo.lock").read_bytes(), b"current\r\n")
            self.assertEqual(git(root, "rev-parse", "HEAD"), self.expected["head"])
            self.assertEqual(git(root, "rev-parse", "HEAD^{tree}"), self.expected["tree"])
            self.assertIs(record, self.expected)
            self.assertEqual(record["guard"]["guard"]["platform"], "linux-x86_64")
            confirm()
        self.assertEqual(set(calls), {"git"})
        self.assertFalse(self.request.destination.exists())
        self.assertEqual((self.repository.root / "Cargo.lock").read_bytes(), b"current\r\n")

    def test_changed_dependency_bytes_refuse_and_clean_the_owned_checkout(self):
        payload = copy.deepcopy(self.payload)
        payload["files"][0]["content"] = "tampered\n"
        machine, public = self.patches()
        with machine, public, self.assertRaisesRegex(Refusal, "complete tree"):
            with self.consume(payload):
                self.fail("changed source cannot run")
        self.assertFalse(self.request.destination.exists())

    def test_foreign_combination_or_source_refuses_before_checkout(self):
        payload = dict(self.payload, snapshot="0" * 64)
        self.assertRaisesRegex(Refusal, "another verified combination", material.qualify, self.request, payload)
        request = snapshot.Request(self.request.source, dict(self.request.identity, marker="v9.0.0"), DOMAIN, self.expected)
        self.assertRaisesRegex(Refusal, "another source", material.qualify, request, self.payload)
        self.assertFalse(self.request.destination.exists())

    def test_unsuccessful_or_foreign_guard_refuses(self):
        for change in ("ok", "plumb", "resolution"):
            expected = copy.deepcopy(self.expected)
            if change == "ok":
                expected["guard"]["ok"] = False
            else:
                expected["guard"]["guard"][change] = "foreign"
            request = snapshot.Request(self.request.source, self.request.identity, DOMAIN, expected)
            with self.subTest(change=change), self.assertRaises(Refusal):
                material.qualify(request, self.payload)

    def test_changed_public_control_refuses_at_entry_and_at_exit(self):
        machine, public = self.patches()
        url = "https://releases.plumb.perish.uk/v1/channels/stable.json"
        with machine, public, self.assertRaisesRegex(Refusal, "latest stable control"):
            with self.consume():
                pointer = json.loads(self.public[url])
                pointer["releaseVersion"] = "v0.88.0"
                self.public[url] = json.dumps(pointer).encode()
        self.assertFalse(self.request.destination.exists())
        machine, public = self.patches()
        with machine, public, self.assertRaisesRegex(Refusal, "latest stable control"):
            with self.consume():
                self.fail("moved authority cannot enter")

    def test_mutation_after_entry_refuses_and_cleans(self):
        machine, public = self.patches()
        with machine, public, self.assertRaisesRegex(Refusal, "tracked content"):
            with self.consume() as (root, _, _):
                (root / "Cargo.lock").write_text("changed")
        self.assertFalse(self.request.destination.exists())

    def test_outside_paths_links_and_unregistered_files_preserve_their_targets(self):
        root = self.repository.root
        for name in ("../Cargo.lock", "/Cargo.lock", "./Cargo.lock", "a//Cargo.lock", "a\\Cargo.lock", "file", "Cargo.lock\0"):
            with self.subTest(name=name), self.assertRaises(Refusal):
                material.path(root, name)
        outside = self.directory / "Cargo.lock"
        outside.write_text("operator")
        (root / "linked").symlink_to(self.directory, target_is_directory=True)
        self.assertRaises(Refusal, material.path, root, "linked/Cargo.lock")
        self.assertEqual(outside.read_text(), "operator")

    def test_payload_budget_duplicates_modes_and_schema_refuse(self):
        row = self.payload["files"][0]
        variants = [None, dict(self.payload, schema="unknown"), dict(self.payload, files=[row, row]), dict(self.payload, files=[dict(row, mode=[])]), dict(self.payload, files=[dict(row, content="x" * (material.LIMIT + 1))]), dict(self.payload, files=[dict(row, content="\ud800")])]
        for payload in variants:
            with self.subTest(payload=type(payload).__name__), self.assertRaises(Refusal):
                material.rows(payload)

    def test_non_arm_host_refuses_before_control_or_source_reads(self):
        with mock.patch.object(material.platform, "machine", return_value="x86_64"), mock.patch.object(material.release, "fetch") as fetch:
            with self.assertRaisesRegex(Refusal, "native Linux ARM64"):
                with self.consume():
                    self.fail("native evidence requires the declared host")
        fetch.assert_not_called()

    def test_compilation_requires_the_actual_prepared_domain_rust(self):
        def rust(source, held, inspect):
            held[0]["rustc"] = {"version": "rustc 9.0.0"}
        machine, public = self.patches()
        with machine, public, mock.patch.object(snapshot, "rust", rust), self.assertRaisesRegex(Refusal, "planned domain Rust"):
            with material.prepared(self.request, self.payload, True):
                self.fail("different Rust cannot compile")

    def test_apk_verifier_uses_material_without_publishing_or_control_installation(self):
        context = dict(self.request.identity, wharf="b" * 40, run="7", attempt="1", snapshot=self.expected, material=self.payload)
        held = dict(context, planned="1", source=str(self.repository.root), target="aarch64-unknown-linux-musl")
        def handler(scoped):
            self.assertEqual((scoped["source"] / "Cargo.lock").read_bytes(), b"current\r\n")
            self.assertNotIn("publication", scoped)
            return scoped["snapshot"]
        machine, public = self.patches()
        with machine, public, mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value={"context": context}), mock.patch.object(execution.toolchain, "versions", return_value=DOMAIN):
            with mock.patch.object(snapshot, "destination", return_value=self.request.destination), mock.patch.object(snapshot, "prepared", side_effect=AssertionError("native controls unavailable")):
                self.assertIs(execution.perform("apk-verify", handler, held), self.expected)
            del context["material"]
            with self.assertRaisesRegex(Refusal, "release material"):
                execution.perform("apk-verify", handler, held)

    def test_native_compilation_unit_prepares_rust_and_musl_without_snapshot_controls(self):
        from scripts import plan as planner
        unit = planner.unit("binary-arm64")
        self.assertEqual(unit["runner"], "ubuntu-24.04-arm")
        self.assertEqual(unit["target"], "aarch64-unknown-linux-musl")
        self.assertEqual(set(unit["prepare"]), {"musl", "rust", "node", "pnpm"})
        self.assertIn("snapshot", planner.unit("apk")["prepare"])
