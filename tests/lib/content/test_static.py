import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.content.static import evidence, runtime, source
from lib.process import git
from lib.refusal import Refusal
from tests.lib.content.test_preview import intent, target
from tests.lib.media.repository import Repository

WORKER = {
    "name": "crest-review", "account_id": "a" * 32, "compatibility_date": "2026-09-30", "workers_dev": False,
    "assets": {"directory": "./dist", "not_found_handling": "404-page"}, "previews": {},
}
PACKAGE = {"name": "crest-review", "private": True, "scripts": {"build": "node build.mjs"}}


class StaticRepository(Repository):
    def __init__(self):
        super().__init__({
            "package.json": json.dumps({"name": "demo", "private": True}),
            "plumb.toml": '[preview.app.crest-review]\npath="apps/review"\npackage="crest-review"\nprovider="cfworker"\naccess="public"\n',
            "apps/review/package.json": json.dumps(PACKAGE), "apps/review/wrangler.jsonc": json.dumps(WORKER),
            "apps/review/build.mjs": "export {};\n", ".gitignore": "dist/\nnode_modules/\n",
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n", ".npmrc": "@perishlab:registry=https://npm.pkg.github.com/\n",
        })
        self.git("remote", "add", "origin", "https://github.com/PerishLab/crest.git")

    def request(self):
        env = evidence.clean(os.environ, tempfile.gettempdir())
        tracked = set(git(self.root, "ls-files").splitlines())
        config = source.configuration(self.root, "crest-review", tracked)
        return dict(intent(), source={"commit": git(self.root, "rev-parse", "HEAD"), "tree": git(self.root, "rev-parse", "HEAD^{tree}"), "declaration": config["digest"]})


def guarded(request, root):
    return {
        "schema": "plumb.guard-runtime/v1", "root": str(root), "ok": True, "strength": "full", "boundary": "head",
        "commit": request["source"]["commit"], "digest": "1" * 64,
        "guard": {"schema": "plumb.guard-proof/v1", "repository": request["repository"], "tree": request["source"]["tree"], "plumb": "v0.66.0@" + "2" * 40, "depot": "3" * 64, "platform": "linux", "digest": "4" * 64, "actions": [{"name": "shape", "input": "5" * 64, "world": "6" * 64}]},
    }


class Qualification(unittest.TestCase):
    def setUp(self):
        self.repository = StaticRepository()
        self.request = self.repository.request()
        self.env = evidence.clean(os.environ, tempfile.gettempdir())

    def test_exact_source_and_target(self):
        config = source.qualify(self.repository.root, self.request, target(), self.env)
        self.assertEqual(config["directory"], "apps/review/dist")
        self.assertEqual(config["digest"], self.request["source"]["declaration"])
        self.assertNotIn("engines", config)

    def test_dirty_untracked_and_changed_head_refuse(self):
        self.repository.write("untracked.html", "content")
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)
        self.repository.commit()
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)

    def test_wrong_request_identity(self):
        for key, value in (("commit", "2" * 40), ("tree", "3" * 40), ("declaration", "4" * 64)):
            request = dict(self.request, source=dict(self.request["source"], **{key: value}))
            with self.subTest(key=key), self.assertRaises(Refusal):
                source.qualify(self.repository.root, request, target(), self.env)

    def test_origin_mismatch(self):
        self.repository.git("remote", "set-url", "origin", "https://example.com/PerishLab/crest.git")
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, self.request, target(), self.env)

    def test_target_mismatch(self):
        changed = dict(target(), worker="production")
        request = dict(self.request, registration=canonical.digest(changed))
        with self.assertRaises(Refusal):
            source.qualify(self.repository.root, request, changed, self.env)

    def test_worker_runtime_and_unknown_configuration_refuse(self):
        for key, value in (("main", "worker.js"), ("routes", []), ("build", {}), ("workers_dev", True), ("previews", {"vars": {}})):
            with self.subTest(key=key), self.assertRaises(Refusal):
                source.worker(json.dumps(dict(WORKER, **{key: value})).encode())

    def test_unsafe_assets_paths(self):
        for value in ("../private", "/tmp/output", ".", "dist//x", "dist/../x", "dist\\x"):
            with self.subTest(value=value), self.assertRaises(Refusal):
                source.directory(value)

    def test_duplicate_worker_identity_refuses(self):
        self.repository.write("apps/other/wrangler.jsonc", json.dumps(WORKER))
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_manifest_and_package_constraints(self):
        for package in (dict(PACKAGE, private=False), dict(PACKAGE, name="other"), dict(PACKAGE, scripts={"build": "node x", "prebuild": "node y"})):
            self.repository.write("apps/review/package.json", json.dumps(package))
            self.repository.commit()
            with self.subTest(package=package), self.assertRaises(Refusal):
                self.repository.request()

    def test_source_symlink_refuses(self):
        path = self.repository.root / "apps/review/package.json"
        path.unlink()
        path.symlink_to("../../../package.json")
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_fresh_checkout_and_output_ancestor(self):
        self.repository.write("node_modules/payload", "unselected")
        with self.assertRaises(Refusal):
            source.fresh(self.repository.root, self.env)
        path = self.repository.root / "apps/review/dist"
        path.symlink_to(tempfile.gettempdir(), target_is_directory=True)
        with self.assertRaises(Refusal):
            source.output(self.repository.root, "apps/review/dist/nested")

    def test_install_config_cannot_redirect_credentials(self):
        self.repository.write(".npmrc", "//example.com/:_authToken=${WHARF_PACKAGES_TOKEN}\n")
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()

    def test_package_filter_cannot_select_multiple_apps(self):
        self.repository.write("apps/other/package.json", json.dumps(PACKAGE))
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.request()


class GuardEvidence(unittest.TestCase):
    def test_full_head_exact_binding(self):
        request = intent()
        proof = guarded(request, Path("/tmp/source"))
        self.assertEqual(evidence.guard(json.dumps(proof), request, "plumb v0.66.0"), proof)
        for key, value in (("ok", False), ("strength", "declared"), ("boundary", "staged"), ("commit", "0" * 40), ("schema", "unknown")):
            with self.subTest(key=key), self.assertRaises(Refusal):
                evidence.guard(json.dumps(dict(proof, **{key: value})), request, "plumb v0.66.0")
        with self.assertRaises(Refusal):
            evidence.guard(json.dumps(proof), request, "plumb v0.65.0")

    def test_json_duplicates_and_bounds_refuse(self):
        for body in ('{"a":1,"a":2}', 'x', ' ' * (evidence.LIMIT + 1)):
            with self.subTest(body=body[:20]), self.assertRaises(Refusal):
                evidence.decode(body)


class GuestEngine:
    def __init__(self, guest, mode="success"):
        self.guest, self.mode, self.owner = guest, mode, ""
        self.identity, self.live, self.calls = "a" * 64, False, []
        self.held = {}

    def world(self):
        return {"image": {"id": "sha256:" + "b" * 64, "resolved": "ghcr.io/perishlab/images@sha256:" + "b" * 64}}

    def prepare(self):
        return None

    def __call__(self, arguments, streaming=False):
        self.calls.append(arguments)
        action = arguments[1]
        if action == "create":
            self.owner = arguments[arguments.index("--label") + 1].split("=")[1]
            self.live = True
            self.held = {"Id": self.identity, "Name": "/wharf-preview-" + self.owner, "Image": "sha256:" + "b" * 64,
                         "State": {"Status": "exited", "Running": False, "ExitCode": 0, "OOMKilled": False},
                         "Config": {"Tty": False, "OpenStdin": False, "User": "1000:1000", "Entrypoint": ["/usr/bin/env"], "Labels": {runtime.LABEL: self.owner}, "Cmd": arguments[arguments.index("ghcr.io/perishlab/images@sha256:" + "b" * 64) + 1:]},
                         "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "NetworkMode": "none", "PidsLimit": 256, "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                                        "Tmpfs": {"/tmp": f'rw,nosuid,nodev,size={runtime.POLICY["temporary"]}'}},
                         "Mounts": [{"Source": str(self.guest.source), "Destination": "/source", "RW": True, "Type": "bind"}, {"Source": str(self.guest.controls), "Destination": "/controls", "RW": False, "Type": "bind"}]}
            if self.mode == "creation-loss":
                raise Refusal("lost creation response")
            if self.mode == "wrong-profile":
                self.held["HostConfig"]["Privileged"] = True
            if self.mode == "wrong-owner":
                self.held["Name"] = "/another-invocation"
            if self.mode == "still-running":
                self.held["State"]["Running"] = True
            if self.mode == "tty":
                self.held["Config"]["Tty"] = True
            return self.identity
        if action == "inspect":
            return json.dumps([self.held])
        if action == "ls":
            if self.mode == "readback-loss":
                raise Refusal("engine unavailable")
            return self.identity if self.live else ""
        if action == "rm":
            if self.mode != "retained":
                self.live = False
            return self.identity
        if action == "start":
            if self.mode == "command-failed":
                raise Refusal("command failed")
            return "selected output"
        raise AssertionError(arguments)


class GuestBoundary(unittest.TestCase):
    def setUp(self):
        self.seat = tempfile.TemporaryDirectory()
        self.addCleanup(self.seat.cleanup)
        root = Path(self.seat.name)
        for name in ("source", "controls"):
            (root / name).mkdir()
        self.guest = runtime.Guest(root / "source", root / "controls", ["node", "build.mjs"], {"CI": "true"})

    def test_fixed_profile_and_confirmed_teardown(self):
        engine = GuestEngine(self.guest)
        result = runtime.run(self.guest, engine)
        self.assertEqual(result.stdout, "selected output")
        self.assertFalse(engine.live)
        self.assertEqual(engine.calls[-1][1], "ls")
        arguments = engine.calls[0]
        self.assertIn("--read-only", arguments)
        self.assertIn("--pull", arguments)
        self.assertIn("label=" + runtime.LABEL + "=" + engine.owner, engine.calls[-1])

    def test_failure_loss_and_unknown_never_return_output(self):
        for mode in ("creation-loss", "wrong-profile", "wrong-owner", "readback-loss", "retained", "command-failed", "still-running", "tty"):
            engine = GuestEngine(self.guest, mode)
            with self.subTest(mode=mode), self.assertRaises(Refusal):
                runtime.run(self.guest, engine)
            if mode in ("creation-loss", "wrong-profile", "command-failed", "tty"):
                self.assertFalse(engine.live)
            if mode == "wrong-owner":
                self.assertFalse(any(call[1] == "rm" for call in engine.calls))

    def test_unsafe_mounts_and_credentials_refuse_before_engine(self):
        bad = [runtime.Guest(Path("/"), self.guest.controls, ["node"], {}),
               runtime.Guest(self.guest.source, self.guest.source, ["node"], {}),
               runtime.Guest(self.guest.source, self.guest.controls, ["node"], {"GH_TOKEN": "forbidden"}),
               runtime.Guest(self.guest.source, self.guest.controls, ["node"], {"NODE_OPTIONS": "forbidden"})]
        linked = Path(self.seat.name) / "linked"
        linked.symlink_to(self.guest.source, target_is_directory=True)
        bad.append(runtime.Guest(linked, self.guest.controls, ["node"], {}))
        for guest in bad:
            engine = GuestEngine(guest)
            with self.subTest(guest=guest), self.assertRaises(Refusal):
                runtime.run(guest, engine)
            self.assertEqual(engine.calls, [])

    def test_host_environment_is_not_inherited(self):
        with mock.patch.dict(os.environ, GH_TOKEN="forbidden", DOCKER_HOST="tcp://forbidden:2375"), mock.patch.object(runtime.shutil, "which", return_value="/bin/true"):
            engine = runtime.Engine(Path(self.seat.name))
        self.assertEqual(set(engine.environment), {"PATH", "HOME"})

    def test_world_drift_refuses_after_teardown(self):
        engine = GuestEngine(self.guest)
        with mock.patch.object(engine, "world", side_effect=[engine.world(), {"changed": True}]), self.assertRaises(Refusal):
            runtime.run(self.guest, engine)
        self.assertFalse(engine.live)

    def test_image_and_engine_authority_refuse_unknown_worlds(self):
        image = {"RepoTags": [runtime.POLICY["image"]], "RepoDigests": ["ghcr.io/perishlab/images@sha256:" + "b" * 64], "Id": "sha256:" + "b" * 64, "Os": "linux", "Architecture": "amd64"}
        server = {"OSType": "linux", "SecurityOptions": ["name=seccomp,profile=builtin"]}
        with mock.patch.object(runtime.shutil, "which", return_value="/bin/true"):
            engine = runtime.Engine(Path(self.seat.name))
        for changed in (dict(image, RepoDigests=[]), dict(image, Os="windows"), dict(image, Architecture="arm64")):
            with mock.patch.object(runtime.Engine, "__call__", side_effect=[json.dumps([changed]), json.dumps(server)]), self.assertRaises(Refusal):
                engine.world()
        with mock.patch.object(runtime.Engine, "__call__", side_effect=[json.dumps([image]), json.dumps(dict(server, SecurityOptions=["name=seccomp,profile=unconfined"]))]), self.assertRaises(Refusal):
            engine.world()

    def test_real_bounded_stream_failure_and_timeout(self):
        with mock.patch.object(runtime.sys, "stderr"):
            self.assertEqual(runtime.attach(["/bin/sh", "-c", "printf selected"], {}, 2), "selected")
            for command in ("exit 17", "sleep 2", "printf overflow", "printf ab; printf cdef >&2", "printf '\\377'"):
                with mock.patch.object(evidence, "LIMIT", 4), self.subTest(command=command), self.assertRaises(Refusal):
                    runtime.attach(["/bin/sh", "-c", command], {}, 0.05)

    def test_separate_stdout_and_drained_diagnostics(self):
        cases = [("printf '{\"ok\":true}'; printf 'diagnostic {\"ok\":false}' >&2", '{"ok":true}'),
                 ("printf diagnostic >&2; printf selected", "selected"),
                 ("printf '\\377' >&2; printf selected", "selected"),
                 ("i=0; while [ $i -lt 10000 ]; do printf noise >&2; i=$((i+1)); done; printf selected", "selected")]
        with mock.patch.object(runtime.sys, "stderr") as logging:
            for command, expected in cases:
                with self.subTest(command=command):
                    self.assertEqual(runtime.attach(["/bin/sh", "-c", command], {}, 2), expected)
            body = "".join(call.args[0] for call in logging.write.call_args_list)
            self.assertIn("diagnostic", body)
            self.assertIn("::stop-commands::", body)


if __name__ == "__main__":
    unittest.main()
