import copy
import hashlib
import json
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from lib.content import implementation
from lib.content.static import bridge, runtime, workspace
from lib.refusal import Refusal
from lib.store import handoff
from tests.lib.content.test_assets import FILES
from tests.lib.content.test_preview import target
from tests.lib.content.test_static import guarded
from tests.lib.media.test_preview import Builder


class Isolated(unittest.TestCase):
    def setUp(self):
        self.fixture = Builder()
        self.request = workspace.Source(self.fixture.repository.root, self.fixture.intent, target())
        self.calls = []
        self.world = {"profile": runtime.POLICY, "image": {"requested": runtime.POLICY["image"], "resolved": runtime.POLICY["image"].removesuffix(":stable") + "@sha256:" + "a" * 64, "id": "sha256:" + "a" * 64},
                      "client": {"path": "/usr/bin/docker", "sha256": "b" * 64},
                      "engine": {"ServerVersion": "fixture", "KernelVersion": "fixture", "Architecture": "x86_64", "SecurityOptions": ["name=seccomp,profile=builtin"]},
                      "implementation": implementation.resourced(["lib.content.static.runtime"], ["build.json"])}

    def execute(self, guest):
        phase = guest.command[-2]
        self.calls.append(guest)
        tools = {name: {"path": "/usr/bin/" + name, "sha256": "c" * 64, "version": "fixture"} for name in ("node", "pnpm", "plumb", "ectropy", "git")}
        for name in ("plumb", "ectropy"):
            tools[name] = {"path": "/controls/" + name, "sha256": hashlib.sha256((guest.controls / name).read_bytes()).hexdigest(), "version": "plumb v0.66.0" if name == "plumb" else "ectropy v0.1.0"}
        if phase == "build":
            directory = guest.source / "apps/review/dist"
            directory.mkdir()
            for name, body in FILES.items():
                (directory / name).write_bytes(body)
        proof = json.dumps(guarded(self.fixture.intent, "/source")) if phase == "guard" else None
        return runtime.Execution(json.dumps({"phase": phase, "tools": tools, "guard": proof}), copy.deepcopy(self.world))

    def build(self, execute=None):
        with mock.patch.object(bridge.shutil, "which", side_effect=lambda name: str(self.fixture.bin / name)):
            return bridge.build(self.request, self.fixture.destination, execute or self.execute)

    def test_three_closed_phases_and_verified_handoff(self):
        result = self.build()
        self.assertEqual(handoff.local(self.fixture.destination), result)
        self.assertEqual([guest.command[-2] for guest in self.calls], ["install", "guard", "build"])
        receipt = result["basis"]["receipt"]
        self.assertEqual(receipt["schema"], "wharf.preview.isolated/v1")
        self.assertEqual(set(receipt["execution"]["phases"]), {"install", "guard", "build"})
        self.assertEqual(receipt["implementation"], handoff.world(True))
        self.assertTrue(all(not guest.source.exists() and not guest.controls.exists() for guest in self.calls))
        self.assertTrue(self.fixture.repository.root.exists())
        for guest in self.calls:
            self.assertNotEqual(guest.source, self.fixture.repository.root)
            self.assertEqual(set(guest.environment), {"PLUMB_GUARD_STRENGTH", "PLUMB_GUARD_BOUNDARY", "CI", "LANG", "LC_ALL"})

    def test_failure_stops_later_phases_and_handoff(self):
        for failed in ("install", "guard", "build"):
            self.calls = []

            def execute(guest):
                if guest.command[-2] == failed:
                    raise Refusal("confirmed failed command")
                return self.execute(guest)

            with self.subTest(phase=failed), self.assertRaises(Refusal):
                self.build(execute)
            self.assertFalse(self.fixture.destination.exists())
            self.assertEqual(len(self.calls), ["install", "guard", "build"].index(failed))

    def test_unknown_teardown_retains_workspace_and_no_handoff(self):
        selected = []

        def execute(guest):
            selected.append(guest.source)
            raise runtime.Uncertain("teardown unknown")

        try:
            with self.assertRaisesRegex(Refusal, "workspace retained"):
                self.build(execute)
            self.assertTrue(selected[0].exists())
            self.assertFalse(self.fixture.destination.exists())
        finally:
            if selected:
                shutil.rmtree(selected[0].parent)

    def test_git_control_mutation_refuses_before_host_git(self):
        def execute(guest):
            (guest.source / ".git/config").write_text("unsafe")
            return self.execute(guest)

        with self.assertRaisesRegex(Refusal, "Git control metadata"):
            self.build(execute)
        self.assertFalse(self.fixture.destination.exists())

    def test_source_control_and_early_output_mutation_refuse(self):
        for changed in ("source", "controls", "output"):
            def execute(guest):
                result = self.execute(guest)
                if changed == "source":
                    (guest.source / "package.json").write_text("{}")
                elif changed == "controls":
                    (guest.controls / "plumb").write_text("changed")
                else:
                    (guest.source / "apps/review/dist").mkdir()
                return result

            with self.subTest(changed=changed), self.assertRaises(Refusal):
                self.build(execute)
            self.assertFalse(self.fixture.destination.exists())

    def test_foreign_incomplete_guard_and_tool_drift_refuse(self):
        for changed in ("root", "commit", "json", "tool"):
            def execute(guest):
                result = self.execute(guest)
                body = json.loads(result.stdout)
                if guest.command[-2] == "guard":
                    if changed == "tool":
                        body["tools"]["node"]["sha256"] = "d" * 64
                    elif changed == "json":
                        body["guard"] = "noise " + body["guard"]
                    else:
                        proof = json.loads(body["guard"])
                        proof[changed] = "/foreign" if changed == "root" else "e" * 40
                        body["guard"] = json.dumps(proof)
                return runtime.Execution(json.dumps(body), result.runtime)

            with self.subTest(changed=changed), self.assertRaises(Refusal):
                self.build(execute)
            self.assertFalse(self.fixture.destination.exists())

    def test_runtime_drift_prevents_handoff(self):
        def execute(guest):
            result = self.execute(guest)
            if guest.command[-2] == "guard":
                result.runtime["image"]["id"] = "changed"
            return result

        with self.assertRaisesRegex(Refusal, "runtime changed"):
            self.build(execute)
        self.assertFalse(self.fixture.destination.exists())

    def test_altered_receipt_and_blob_refuse_independent_consumer(self):
        result = self.build()
        altered = copy.deepcopy(result)
        altered["basis"]["receipt"]["execution"]["controls"]["plumb"] = "f" * 64
        with self.assertRaises(Refusal):
            handoff.verify(altered, lambda name: (self.fixture.destination / name).read_bytes())
        name = next(iter(result["files"].values()))["blob"]
        (self.fixture.destination / name).write_bytes(b"changed")
        with self.assertRaises(Refusal):
            handoff.local(self.fixture.destination)

    def test_incomplete_runtime_identity_refuses(self):
        for field in ("image", "engine", "client", "implementation"):
            self.world[field] = {}
            with self.subTest(field=field), self.assertRaises(Refusal):
                self.build()
            self.assertFalse(self.fixture.destination.exists())

    def test_guard_generated_objects_and_output_are_not_build_output(self):
        def execute(guest):
            result = self.execute(guest)
            if guest.command[-2] == "guard":
                body = b"blob 5\x00guard"
                digest = hashlib.sha1(body).hexdigest()
                entry = guest.source / ".git/objects" / digest[:2] / digest[2:]
                entry.parent.mkdir(exist_ok=True)
                entry.write_bytes(zlib.compress(body))
                output = guest.source / "apps/review/dist"
                output.mkdir()
                (output / "index.html").write_bytes(b"guard output")
            elif guest.command[-2] == "build":
                self.assertEqual((guest.source / "apps/review/dist/index.html").read_bytes(), FILES["index.html"])
            return result

        result = self.build(execute)
        self.assertEqual(result, handoff.local(self.fixture.destination))

    def test_foreign_git_object_refuses(self):
        def execute(guest):
            result = self.execute(guest)
            entry = guest.source / ".git/objects/aa" / ("b" * 38)
            entry.parent.mkdir(exist_ok=True)
            entry.write_bytes(zlib.compress(b"blob 5\x00guard"))
            return result

        with self.assertRaisesRegex(Refusal, "content identity"):
            self.build(execute)
        self.assertFalse(self.fixture.destination.exists())


if __name__ == "__main__":
    unittest.main()
