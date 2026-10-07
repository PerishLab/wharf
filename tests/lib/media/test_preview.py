import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.content.static import evidence, workspace
from lib.media import node
from lib.refusal import Refusal
from lib.store import handoff
from tests.lib.content.test_assets import FILES
from tests.lib.content.test_preview import target
from tests.lib.content.test_static import StaticRepository, guarded

VERSIONS = {"node": "v24.18.0", "pnpm.cjs": "11.13.0", "pnpm": "11.13.0", "plumb": "plumb v0.66.0", "ectropy": "ectropy v0.1.0", "git": "git version 2.0.0"}


class Builder:
    def __init__(self):
        self.repository = StaticRepository()
        self.intent = self.repository.request()
        self.destination = Path(tempfile.mkdtemp()) / "handoff"
        self.calls = []
        self.proof = guarded(self.intent, self.repository.root)
        self.bin = Path(tempfile.mkdtemp())
        for name in ("node", "pnpm", "plumb", "ectropy"):
            path = self.bin / name
            path.write_bytes(f"fixture-{name}".encode())
            path.chmod(0o755)

    def inspect(self, argv, cwd, env):
        self.calls.append(("inspect", list(argv), dict(env)))
        return VERSIONS[Path(argv[0]).name] if "--version" in argv else json.dumps(self.proof)

    def execute(self, argv, cwd, env):
        self.calls.append(("execute", list(argv), dict(env)))
        if "build" in argv:
            root = self.repository.root / "apps/review/dist"
            root.mkdir()
            for name, body in FILES.items():
                (root / name).write_bytes(body)

    def build(self, environ=None):
        request = node.Preview(self.repository.root, self.destination, self.intent, target())
        return node.preview_build(request, self.execute, self.inspect, self.environment(environ))

    def environment(self, environ=None):
        return dict(environ or os.environ, PATH=f"{self.bin}:/usr/bin:/bin")


class CredentialBoundary(unittest.TestCase):
    def test_allowlisted_environments_and_fixed_commands(self):
        builder = Builder()
        env = dict(os.environ, CLOUDFLARE_API_TOKEN="forbidden", WHARF_R2_SECRET_ACCESS_KEY="forbidden", GH_TOKEN="forbidden", NODE_OPTIONS="forbidden", NPM_TOKEN="forbidden", WHARF_PACKAGES_TOKEN="read-only")
        document = builder.build(env)
        self.assertEqual(handoff.local(builder.destination), document)
        calls = [(argv, env) for kind, argv, env in builder.calls if kind == "execute"]
        self.assertEqual(calls[0][0][1:], ["install", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile"])
        self.assertEqual(calls[1][0][1:], ["--filter", "crest-review", "run", "build"])
        self.assertEqual(calls[0][1][node.READER], "read-only")
        self.assertNotIn(node.READER, calls[1][1])
        for kind, argv, child in builder.calls:
            with self.subTest(kind=kind, command=argv[1]):
                self.assertFalse({"CLOUDFLARE_API_TOKEN", "WHARF_R2_SECRET_ACCESS_KEY", "GH_TOKEN", "NODE_OPTIONS", "NPM_TOKEN"} & set(child))
                self.assertNotEqual(child["HOME"], str(Path.home()))
                if kind == "inspect":
                    self.assertNotIn(node.READER, child)
        self.assertFalse(Path(calls[0][1]["NPM_CONFIG_USERCONFIG"]).exists())

    def test_guard_selectors_are_explicit_and_no_source_receipt_is_trusted(self):
        builder = Builder()
        result = builder.build()
        guard_call = next(env for kind, argv, env in builder.calls if "guard" in argv)
        self.assertEqual((guard_call["PLUMB_GUARD_STRENGTH"], guard_call["PLUMB_GUARD_BOUNDARY"]), ("full", "head"))
        self.assertEqual(result["basis"]["receipt"]["source"]["commit"], builder.intent["source"]["commit"])
        self.assertNotIn("request", result["basis"]["receipt"])

    def test_dirty_source_refuses_before_product_execution(self):
        builder = Builder()
        builder.repository.write("untracked", "change")
        with self.assertRaises(Refusal):
            builder.build()
        self.assertFalse(any(kind == "execute" for kind, _, _ in builder.calls))

    def test_guard_refusal_prevents_build_and_handoff(self):
        builder = Builder()
        builder.proof["ok"] = False
        with self.assertRaises(Refusal):
            builder.build()
        self.assertFalse(any("build" in argv for _, argv, _ in builder.calls))
        self.assertFalse(builder.destination.exists())

    def test_source_mutation_in_build_refuses(self):
        builder = Builder()
        original = builder.execute

        def execute(argv, cwd, env):
            original(argv, cwd, env)
            if "build" in argv:
                builder.repository.write("apps/review/build.mjs", "changed")

        request = node.Preview(builder.repository.root, builder.destination, builder.intent, target())
        with self.assertRaises(Refusal):
            node.preview_build(request, execute, builder.inspect, builder.environment())
        self.assertFalse(builder.destination.exists())

    def test_unsafe_output_refuses(self):
        builder = Builder()
        original = builder.execute

        def execute(argv, cwd, env):
            original(argv, cwd, env)
            if "build" in argv:
                (builder.repository.root / "apps/review/dist/worker.js").write_text("run()")

        request = node.Preview(builder.repository.root, builder.destination, builder.intent, target())
        with self.assertRaises(Refusal):
            node.preview_build(request, execute, builder.inspect, builder.environment())

    def test_existing_output_and_handoff_refuse(self):
        builder = Builder()
        builder.repository.write("apps/review/dist/index.html", "old bytes")
        with self.assertRaises(Refusal):
            builder.build()
        self.assertFalse(any(kind == "execute" for kind, _, _ in builder.calls))
        request = node.Preview(builder.repository.root, builder.repository.root / "out", builder.intent, target())
        with self.assertRaises(Refusal):
            node.preview_build(request, builder.execute, builder.inspect, builder.environment())

    def test_wrong_node_world_refuses_before_install(self):
        builder = Builder()
        versions = dict(VERSIONS, node="v22.0.0")

        def inspect(argv, cwd, env):
            return versions[Path(argv[0]).name] if "--version" in argv else builder.inspect(argv, cwd, env)

        request = node.Preview(builder.repository.root, builder.destination, builder.intent, target())
        with self.assertRaises(Refusal):
            node.preview_build(request, builder.execute, inspect, builder.environment())
        self.assertFalse(any(kind == "execute" for kind, _, _ in builder.calls))

    def test_changed_tool_and_implementation_refuse(self):
        builder = Builder()
        with mock.patch.object(evidence, "unchanged", side_effect=Refusal("tool changed")), self.assertRaises(Refusal):
            builder.build()
        builder = Builder()
        actual = handoff.world()
        modified = copy.deepcopy(actual)
        modified["lib.media.node"] = "0" * 64
        with mock.patch.object(handoff, "world", side_effect=[actual, modified]), self.assertRaises(Refusal):
            builder.build()

    def test_clean_environment_is_not_a_provider_write_scope(self):
        env = evidence.clean({"PATH": "/usr/bin:/bin", "UNKNOWN_SECRET": "forbidden", "PLUMB_HOME": "/private"}, "/tmp/isolated")
        self.assertNotIn("UNKNOWN_SECRET", env)
        self.assertNotIn("PLUMB_HOME", env)

    def test_real_product_child_does_not_inherit_outer_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            env = evidence.clean(dict(os.environ, CLOUDFLARE_API_TOKEN="forbidden"), directory)
            command = ["/bin/sh", "-c", 'test -z "$CLOUDFLARE_API_TOKEN"']
            node.preview_execute(command, directory, env)

    def test_product_descendants_are_stopped_after_the_command(self):
        with tempfile.TemporaryDirectory() as directory:
            env = evidence.clean(os.environ, directory)
            command = ["/bin/sh", "-c", "(sleep 0.2; touch late-write) & exit 0"]
            node.preview_execute(command, directory, env)
            time.sleep(0.3)
            self.assertFalse((Path(directory) / "late-write").exists())


class DisposableSource(unittest.TestCase):
    def setUp(self):
        self.repository = StaticRepository()
        self.request = workspace.Source(self.repository.root, self.repository.request(), target())

    def test_exact_independent_checkout_and_cleanup(self):
        with workspace.prepare(self.request) as seat:
            staged = seat.root
            self.assertEqual(seat.configuration["digest"], self.request.intent["source"]["declaration"])
            for name in ("package.json", ".git/HEAD"):
                self.assertFalse(os.path.samefile(self.repository.root / name, staged / name))
            objects = list((staged / ".git/objects").rglob("*.pack"))
            self.assertTrue(objects)
            self.assertTrue(all(path.stat().st_nlink == 1 for path in objects))
            self.assertFalse((staged / ".git/objects/info/alternates").exists())
            self.assertIn("https://github.com/PerishLab/crest.git", (staged / ".git/config").read_text())
            self.assertNotIn(str(self.repository.root), (staged / ".git/config").read_text())
        self.assertFalse(staged.exists())
        self.assertTrue(self.repository.root.exists())

    def test_local_config_hooks_and_host_environment_are_not_copied(self):
        hook = self.repository.root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch should-not-run\n")
        hook.chmod(0o755)
        self.repository.git("config", "credential.helper", "forbidden")
        self.repository.git("config", "core.fsmonitor", "forbidden")
        with mock.patch.dict(os.environ, GH_TOKEN="forbidden", GIT_CONFIG_COUNT="999", PATH="/forbidden"):
            with workspace.prepare(self.request) as seat:
                self.assertFalse((seat.root / "should-not-run").exists())
                self.assertFalse((seat.root / ".git/hooks/post-checkout").exists())
                self.assertNotIn("forbidden", (seat.root / ".git/config").read_text())
        env = workspace.environment("/tmp/home")
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_writes_and_git_changes_cannot_reach_original(self):
        original = (self.repository.root / "package.json").read_bytes()
        with workspace.prepare(self.request) as seat:
            (seat.root / "package.json").write_text("changed")
            (seat.root / ".git/HEAD").write_text("changed")
        self.assertEqual((self.repository.root / "package.json").read_bytes(), original)
        self.assertEqual(self.repository.request(), self.request.intent)

    def test_dirty_ignored_and_stale_source_refuse(self):
        for name in ("untracked", "node_modules/payload"):
            with self.subTest(name=name):
                self.repository.write(name, "unselected")
                with self.assertRaises(Refusal), workspace.prepare(self.request):
                    self.fail("unsafe source was admitted")
                (self.repository.root / name).unlink()
        self.repository.commit()
        with self.assertRaises(Refusal), workspace.prepare(self.request):
            self.fail("stale source was admitted")

    def test_linked_or_relative_source_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            linked = Path(directory) / "source"
            linked.symlink_to(self.repository.root, target_is_directory=True)
            for root in (linked, Path("relative")):
                with self.subTest(root=root), self.assertRaises(Refusal), workspace.prepare(workspace.Source(root, self.request.intent, self.request.target)):
                    self.fail("unsafe source seat was admitted")

    def test_preparation_error_and_caller_failure_remove_only_the_seat(self):
        with self.assertRaises(RuntimeError):
            with workspace.prepare(self.request) as seat:
                root = seat.root
                raise RuntimeError("caller failed")
        self.assertFalse(root.exists())
        self.assertTrue(self.repository.root.exists())
        with mock.patch.object(workspace, "independent", side_effect=Refusal("uncertain storage")), self.assertRaises(Refusal):
            with workspace.prepare(self.request):
                self.fail("uncertain storage was admitted")


if __name__ == "__main__":
    unittest.main()
