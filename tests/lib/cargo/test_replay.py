import json
import os
import re
import http.client
import http.server
import threading
import tomllib
import urllib.request
import unittest
import unittest.mock
from types import SimpleNamespace

from lib.content import canonical
from lib.identity.guard import replay
from lib.store import plan
from lib.process import git
from lib.refusal import Refusal
from tests.lib.cargo.test_basis import Repository

OWN = """
[[package]]
name = "demo"
version = "0.9.0"
source = "sparse+https://cargo.perish.uk/"
checksum = "eeee"
"""


def index(**versions):
    def reader(url):
        name = url.rsplit("/", 1)[1]
        return "\n".join(json.dumps({"name": name, "vers": version, "yanked": False}) for version in versions.get(name, []))
    return reader


class Replay(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.repository.edit("Cargo.lock", 'dependencies = ["demo"]', 'dependencies = ["demo 0.0.0", "demo 0.9.0"]')
        self.repository.write("Cargo.lock", (self.repository.root / "Cargo.lock").read_text() + OWN)
        self.repository.commit()
        self.root = self.repository.root
        self.expected = {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}]}

    def held(self, expected):
        request = SimpleNamespace(expected=expected, identity={"marker": "v1.0.0"})
        return {"request": request, "root": self.root, "tools": {"cargo": {"path": "cargo"}, "git": {"path": "git"}}}

    def test_own_library_published_by_this_marker_still_replays(self):
        replay.current(self.root, self.expected, "1.0.0", index(demo=["0.9.0", "1.0.0-rc.1", "1.0.0"]))

    def test_foreign_latest_movement_refuses(self):
        expected = {"packages": self.expected["packages"] + [{"ecosystem": "cargo", "name": "plumb", "version": "0.1.0"}]}
        with self.assertRaisesRegex(Refusal, "plumb moved to 0.2.0; prepare a fresh plan"):
            replay.current(self.root, expected, "1.0.0", index(demo=["1.0.0"], plumb=["0.1.0", "0.2.0"]))

    def test_own_library_moving_elsewhere_refuses(self):
        with self.assertRaisesRegex(Refusal, "demo moved to 1.1.0"):
            replay.current(self.root, self.expected, "1.0.0", index(demo=["0.9.0", "1.1.0"]))

    def test_replay_keeps_the_planned_lock_and_tree(self):
        calls = []
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        execute = lambda held, argv: calls.append(argv)
        run = lambda held, *args: git(self.root, *args).strip()
        with unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0", "1.0.0"])):
            result = replay.resolved(self.held(self.expected), execute, run)
        self.assertEqual(calls, [])
        self.assertEqual(result, {"context": "lift", "tree": tree, "packages": self.expected["packages"]})

    def test_replay_moves_a_package_the_plan_resolved_elsewhere(self):
        expected = {"packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.1"}]}
        self.assertEqual(replay.moves(self.root, expected), [("demo", "0.9.0")])

    def test_applies_only_to_planned_cargo_snapshots(self):
        self.assertTrue(replay.applies(self.held(self.expected)))
        self.assertFalse(replay.applies(self.held(None)))
        self.assertFalse(replay.applies(self.held({"packages": [{"ecosystem": "npm", "name": "x", "version": "1"}]})))



THIRD = """
[[package]]
name = "tempfile"
version = "3.27.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
dependencies = [
 "getrandom 0.4.3",
]
"""


class Sibling(unittest.TestCase):
    def setUp(self):
        fixture = Replay()
        fixture.setUp()
        self.root = fixture.root
        self.held = fixture.held
        lock = self.root / "Cargo.lock"
        lock.write_text(lock.read_text() + THIRD + self.entries("0.103.6"))
        fixture.repository.commit()
        self.expected = {"packages": fixture.expected["packages"] + [{"ecosystem": "cargo", "name": name, "version": "0.103.7"} for name in ("plumb", "plumb-macro")]}
        self.published = "0.103.7"
        self.calls = []

    def entries(self, version):
        return "".join(f'\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = "{replay.SOURCE}"\n' for name in ("plumb", "plumb-macro"))

    def cargo(self, held, argv):
        self.calls.append(argv)
        lock = self.root / "Cargo.lock"
        text = re.sub(r'\n\[\[package\]\]\nname = "[^"]+"\nversion = "[^"]+"\nsource = "' + re.escape(replay.SOURCE) + r'"\n(checksum = "[^"]+"\n)?', "", lock.read_text())
        if argv[1] == "update":
            text = text.replace("getrandom 0.4.3", "getrandom 0.3.4")
        lock.write_text(text + OWN + self.entries(self.published))

    def resolve(self, execute):
        run = lambda held, *args: git(self.root, *args).strip()
        with unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0"], plumb=["0.103.7"], **{"plumb-macro": ["0.103.7"]})):
            return replay.resolved(self.held(self.expected), execute, run)

    def test_one_move_that_also_moves_a_later_package_replays_without_touching_third_party_edges(self):
        result = self.resolve(self.cargo)
        self.assertEqual(self.calls, [["cargo", "metadata", "--format-version", "1"]])
        self.assertEqual(replay.moves(self.root, self.expected), [])
        self.assertIn(THIRD, (self.root / "Cargo.lock").read_text())
        self.assertEqual(result["packages"], self.expected["packages"])

    def test_resolution_drops_only_first_party_entries(self):
        read = lambda: tomllib.loads((self.root / "Cargo.lock").read_text())
        before = read()
        replay.dropped(self.root)
        after = read()
        self.assertEqual(after["package"], [item for item in before["package"] if item.get("source") != replay.SOURCE])
        self.assertEqual(after["version"], before["version"])
        self.assertIn(THIRD, (self.root / "Cargo.lock").read_text())

    def test_a_cargo_failure_is_not_suppressed(self):
        def refuse(held, argv):
            raise Refusal("failed to select a version")
        with self.assertRaisesRegex(Refusal, "failed to select a version"):
            self.resolve(refuse)

    def test_a_resolution_outside_the_plan_refuses(self):
        self.published = "0.103.8"
        with self.assertRaisesRegex(Refusal, "cargo resolved plumb 0.103.8 outside the plan; prepare a fresh plan"):
            self.resolve(self.cargo)


class Reserved(unittest.TestCase):
    def setUp(self):
        from tests.lib.store.memory import Memory
        self.bucket = Memory()
        self.context = {"repository": "PerishLab/demo", "marker": "v1.0.0", "run": "9", "attempt": "1"}
        self.combination = {"schema": "wharf.release.snapshot/v1", "source": {"repository": "PerishLab/demo", "marker": "v1.0.0", "commit": "a" * 40, "tree": "b" * 40}, "head": "c" * 40, "tree": "d" * 40, "packages": [{"ecosystem": "cargo", "name": "demo", "version": "0.9.0"}], "controls": {}, "domain": {}}

    def test_a_marker_that_began_publication_replans_its_reserved_combination(self):
        self.bucket.create(plan.publication(self.context), canonical.encode(self.combination))
        plan.remember(self.bucket, dict(self.context, snapshot=dict(self.combination, guard={"digest": "e"})))
        recalled = {}
        self.assertEqual(plan.reserved(self.bucket, self.context, recalled), dict(self.combination, guard={"digest": "e"}))
        self.assertEqual(recalled["run"], "9")

    def test_a_fresh_marker_resolves_latest(self):
        self.assertIsNone(plan.reserved(self.bucket, self.context, {}))


class Mixed(unittest.TestCase):
    def setUp(self):
        fixture = Replay()
        fixture.setUp()
        self.root = fixture.root
        self.expected = fixture.expected
        fixture.repository.write("plumb.toml", '[release.npm]\npackages = ["@perishlab/owned"]\n')
        fixture.repository.commit()
        self.expected["packages"].append({"ecosystem": "npm", "name": "@perishlab/demo", "version": "0.9.0"})
        self.expected["tree"] = git(self.root, "rev-parse", "HEAD^{tree}").strip()
        self.calls = []
        self.latest = "0.9.0"

    def held(self, expected):
        held = Replay.held(self, expected)
        held["tools"]["pnpm"] = {"path": "/prepared/pnpm"}
        held["env"] = {"USERPROFILE": str(self.root.parent)}
        return held

    def execute(self, held, argv):
        raise AssertionError("unchanged npm replay never invokes product processes")

    def reader(self, name, location):
        self.calls.append((name, location))
        return {"name": name, "dist-tags": {"latest": self.latest}, "versions": {self.latest: {"name": name, "version": self.latest}}}

    def resolve(self):
        run = lambda held, *args: git(self.root, *args).strip()
        with unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0"])):
            with unittest.mock.patch.object(replay, "metadata", side_effect=self.reader):
                return replay.resolved(self.held(self.expected), self.execute, run)

    def test_mixed_replay_reads_only_the_declared_registry_without_product_processes(self):
        self.assertTrue(replay.applies(self.held(self.expected)))
        self.assertEqual(self.resolve()["tree"], self.expected["tree"])
        self.assertEqual(self.calls, [("@perishlab/demo", "https://npm.pkg.github.com/")])

    def test_own_npm_publication_by_this_marker_still_replays(self):
        self.expected["packages"][-1]["name"] = "@perishlab/owned"
        self.latest = "1.0.0"
        self.assertEqual(self.resolve()["tree"], self.expected["tree"])

    def test_mixed_replay_refuses_a_different_planned_tree(self):
        self.expected["tree"] = "0" * 40
        with self.assertRaisesRegex(Refusal, "exact planned npm"):
            self.resolve()

    def test_mixed_replay_refuses_foreign_latest_drift(self):
        self.expected["packages"][-1]["version"] = "0.8.0"
        with self.assertRaisesRegex(Refusal, "moved; prepare a fresh plan"):
            self.resolve()

    def test_mixed_replay_refuses_unknown_registry_before_execution(self):
        self.expected["packages"][-1]["name"] = "@foreign/demo"
        with self.assertRaisesRegex(Refusal, "unsupported npm registry"):
            self.resolve()
        self.assertEqual(self.calls, [])

    def test_mixed_replay_refuses_nonstable_metadata(self):
        run = lambda held, *args: git(self.root, *args).strip()
        for result in (None, "1.0.0-rc.1", ["0.9.0"], {"dist-tags": None}, {"dist-tags": {"latest": "0.9.0"}, "versions": None}):
            with self.subTest(result=result), unittest.mock.patch.object(replay.registry, "fetch", index(demo=["0.9.0"])):
                with unittest.mock.patch.object(replay, "metadata", return_value=result):
                    with self.assertRaisesRegex(Refusal, "current stable npm metadata"):
                        replay.resolved(self.held(self.expected), self.execute, run)


class Registry(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.body = b'{"name":"@perishlab/demo","dist-tags":{"latest":"0.9.0"},"versions":{"0.9.0":{"name":"@perishlab/demo","version":"0.9.0"}}}'
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(owner.status)
                if owner.status == 302:
                    self.send_header("Location", "https://outside.invalid/leak")
                self.end_headers()
                self.wfile.write(owner.body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.enterContext(unittest.mock.patch.dict(os.environ, {"WHARF_PACKAGES_TOKEN": "prepared-reader", "NODE_AUTH_TOKEN": "writer-must-not-pass"}))
        port = self.server.server_port

        class Local(urllib.request.HTTPSHandler):
            def https_open(self, request):
                return self.do_open(lambda host, **args: http.client.HTTPConnection("127.0.0.1", port, timeout=args["timeout"]), request)

        self.enterContext(unittest.mock.patch.object(urllib.request, "HTTPSHandler", Local))

    def read(self):
        return replay.metadata("@perishlab/demo", "https://npm.pkg.github.com/")

    def test_real_http_uses_only_the_prepared_reader_and_canonical_package(self):
        self.assertEqual(self.read()["dist-tags"]["latest"], "0.9.0")
        self.assertEqual(self.requests, [("/@perishlab%2Fdemo", "Bearer prepared-reader")])

    def test_redirect_is_refused_without_forwarding_credentials(self):
        self.status = 302
        with self.assertRaisesRegex(Refusal, "HTTP 302"):
            self.read()
        self.assertEqual(len(self.requests), 1)

    def test_missing_reader_and_foreign_registry_refuse_before_network(self):
        with self.assertRaisesRegex(Refusal, "unsupported registry"):
            replay.metadata("@perishlab/demo", "https://outside.invalid/")
        with unittest.mock.patch.dict(os.environ, {"WHARF_PACKAGES_TOKEN": ""}):
            with self.assertRaisesRegex(Refusal, "prepared package reader"):
                self.read()
        self.assertEqual(self.requests, [])

    def test_error_and_oversize_body_do_not_become_metadata_or_diagnostics(self):
        self.status = 401
        self.body = b"must-not-be-in-diagnostics"
        with self.assertRaisesRegex(Refusal, "HTTP 401") as found:
            self.read()
        self.assertNotIn(self.body.decode(), str(found.exception))
        self.status = 200
        self.body = b"x" * (replay.evidence.LIMIT + 1)
        with self.assertRaisesRegex(Refusal, "budget"):
            self.read()
