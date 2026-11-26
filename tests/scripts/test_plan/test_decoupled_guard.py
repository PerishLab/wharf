import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity import guard
from lib.identity.guard import configuration, execution, mismatch, snapshot
from lib.media import depot, release
from lib.refusal import Refusal
from scripts import plan
from tests.lib.store.memory import Memory
from tests.scripts.test_plan.test_configuration import BASE, depot_world, make_generation

DOMAIN = {"node.version": "24.18.0", "pnpm.version": "11.28.5", "rust.version": "1.96.1"}
PLUMB_VERSION = "v0.90.0"
ECTROPY_VERSION = "v0.20.0"
COMMIT = "2" * 40
LEGACY_DEPOT = "3" * 64


def seal_routes(plumb_guard):
    routes = {}
    for name, version in (("plumb", PLUMB_VERSION), ("ectropy", ECTROPY_VERSION)):
        authority = mismatch.RELEASES["authority"].format(name=name)
        seal = {"product": name, "releaseVersion": version, "commit": COMMIT}
        if name == "plumb":
            seal["guard"] = plumb_guard
        body = json.dumps(seal).encode()
        url = f"{authority}/v1/releases/stable/{version}/seal.json"
        routes[url] = body
        routes[f"{authority}/v1/channels/stable.json"] = json.dumps(dict(seal, schema=1, channel="stable", seal={"url": url, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})).encode()
    return routes


def make_pinned(version=PLUMB_VERSION, body=b"level = 1\n", prior=None, created="2026-11-25T00:00:00Z"):
    ptr, _, raw = make_generation(version, body, prior, created)
    pinned = {"channel": "stable", "version": version, "generation": ptr["generation"], "manifest": ptr["manifest"]}
    return pinned, ptr, raw


class MismatchStable(unittest.TestCase):
    def test_legacy_mode_verifies_seal_depot_and_rejects_pinned_configuration(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        routes = seal_routes({"producer": producer, "depot": LEGACY_DEPOT})
        controls = {"plumb": f"plumb {PLUMB_VERSION}", "ectropy": f"ectropy {ECTROPY_VERSION}", "authority": {"producer": producer, "depot": LEGACY_DEPOT}}
        observed = mismatch.stable(controls, routes.__getitem__)
        self.assertEqual(observed["plumb"], {"releaseVersion": PLUMB_VERSION, "commit": COMMIT})
        pinned, _, _ = make_pinned()
        with self.assertRaises(Refusal):
            mismatch.stable(controls, routes.__getitem__, pinned)

    def test_decoupled_mode_requires_valid_pinned_and_ignores_floating_latest_drift(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        pinned, ptr, raw = make_pinned()
        _, newer_ptr, _ = make_pinned("v0.91.0", b"level = 2\n", ptr["generation"], "2026-11-25T01:00:00Z")
        routes = seal_routes({"producer": producer})
        routes[pinned["manifest"]["url"]] = raw
        routes[f"{BASE}/channels/stable/configurations/latest.json"] = depot.pretty(newer_ptr)
        controls = {"plumb": f"plumb {PLUMB_VERSION}", "ectropy": f"ectropy {ECTROPY_VERSION}", "authority": {"producer": producer, "depot": pinned["generation"]}}
        observed = mismatch.stable(controls, routes.__getitem__, pinned)
        self.assertEqual(observed["plumb"], {"releaseVersion": PLUMB_VERSION, "commit": COMMIT})
        with self.assertRaises(Refusal):
            mismatch.stable(controls, routes.__getitem__, None)
        wrong_controls = dict(controls, authority={"producer": producer, "depot": "0" * 64})
        with self.assertRaises(Refusal):
            mismatch.stable(wrong_controls, routes.__getitem__, pinned)
        tampered = dict(routes, **{pinned["manifest"]["url"]: b"tampered"})
        with self.assertRaises(Refusal):
            mismatch.stable(controls, tampered.__getitem__, pinned)


class SnapshotControls(unittest.TestCase):
    def held(self, authority_reply, **options):
        seen = []

        def inspect(argv, cwd, env):
            seen.append((argv, dict(env)))
            if argv[1:] == ["metadata", "--json"]:
                return json.dumps(DOMAIN)
            if argv[1:] == ["release", "authority", "--json"]:
                return json.dumps(authority_reply)
            if argv[1:] == ["configuration", "install"]:
                return ""
            raise AssertionError(argv)

        identity = {"repository": "PerishLab/example", "marker": "v1.0.0", "commit": "a" * 40, "tree": "b" * 40}
        request = snapshot.Request(Path("/source"), identity, DOMAIN, expected=options.get("expected"), configuration=options.get("pinned"), captured=options.get("captured"))
        tools = {"plumb": {"path": "/bin/plumb", "version": f"plumb {PLUMB_VERSION}"}, "ectropy": {"path": "/bin/ectropy", "version": f"ectropy {ECTROPY_VERSION}"}}
        return {"request": request, "root": Path("/work"), "env": {}, "tools": tools, "inspect": inspect, "seen": seen}

    def test_fresh_plan_in_decoupled_mode_discovers_once_and_binds_authority_without_extra_keys(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        pinned, ptr, raw = make_pinned()
        routes = seal_routes({"producer": producer}) | depot_world(ptr, ptr, {ptr["manifest"]["url"]: raw})
        captured = {}
        held = self.held({"producer": producer}, captured=captured)
        with mock.patch.object(configuration, "discover", wraps=configuration.discover) as discover:
            world = snapshot.controls(held, routes.__getitem__)
        discover.assert_called_once()
        self.assertEqual(set(world), {"plumb", "ectropy", "authority", "stable"})
        self.assertEqual(world["authority"], {"producer": producer, "depot": pinned["generation"]})
        self.assertEqual(captured["configuration"], pinned)
        encoded = canonical.encode(pinned).decode()
        self.assertEqual(held["env"]["PLUMB_CONFIGURATION"], encoded)
        snapshot.execute(held, [held["tools"]["plumb"]["path"], "configuration", "install"])
        self.assertEqual(held["seen"][-1][1]["PLUMB_CONFIGURATION"], encoded)

    def test_decoupled_mode_accepts_transitional_depot_when_matching_and_refuses_mismatch(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        pinned, ptr, raw = make_pinned()
        routes = seal_routes({"producer": producer}) | depot_world(ptr, ptr, {ptr["manifest"]["url"]: raw})
        world = snapshot.controls(self.held({"producer": producer, "depot": pinned["generation"]}), routes.__getitem__)
        self.assertEqual(world["authority"]["depot"], pinned["generation"])
        with self.assertRaisesRegex(Refusal, "pinned configuration generation"):
            snapshot.controls(self.held({"producer": producer, "depot": "9" * 64}), routes.__getitem__)

    def test_downstream_job_verifies_pinned_even_after_floating_latest_moves(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        pinned, ptr, raw = make_pinned()
        _, newer_ptr, _ = make_pinned("v0.91.0", b"level = 2\n", ptr["generation"], "2026-11-25T01:00:00Z")
        routes = seal_routes({"producer": producer})
        routes[pinned["manifest"]["url"]] = raw
        routes[f"{BASE}/channels/stable/configurations/latest.json"] = depot.pretty(newer_ptr)
        held = self.held({"producer": producer}, pinned=pinned, expected={"schema": "wharf.release.snapshot/v1"})
        with mock.patch.object(configuration, "discover", side_effect=AssertionError("downstream must not discover")):
            world = snapshot.controls(held, routes.__getitem__)
        self.assertEqual(world["authority"], {"producer": producer, "depot": pinned["generation"]})
        self.assertEqual(held["env"]["PLUMB_CONFIGURATION"], canonical.encode(pinned).decode())
        missing = self.held({"producer": producer}, pinned=None, expected={"schema": "wharf.release.snapshot/v1"})
        with mock.patch.object(configuration, "discover", side_effect=AssertionError("downstream must not discover")), self.assertRaisesRegex(Refusal, "decoupled Plumb seal requires a pinned configuration"):
            snapshot.controls(missing, routes.__getitem__)

    def test_legacy_mode_keeps_identical_behavior_and_refuses_pinned_override(self):
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        routes = seal_routes({"producer": producer, "depot": LEGACY_DEPOT})
        captured = {}
        held = self.held({"producer": producer, "depot": LEGACY_DEPOT}, captured=captured)
        world = snapshot.controls(held, routes.__getitem__)
        self.assertEqual(world["authority"], {"producer": producer, "depot": LEGACY_DEPOT})
        self.assertEqual(captured, {})
        self.assertNotIn("PLUMB_CONFIGURATION", held["env"])
        snapshot.execute(held, [held["tools"]["plumb"]["path"], "configuration", "install"])
        self.assertNotIn("PLUMB_CONFIGURATION", held["seen"][-1][1])
        pinned, _, _ = make_pinned()
        with self.assertRaisesRegex(Refusal, "legacy Plumb seal"):
            snapshot.controls(self.held({"producer": producer, "depot": LEGACY_DEPOT}, pinned=pinned), routes.__getitem__)


class ExecutionAndPlan(unittest.TestCase):
    def setUp(self):
        self.pinned, _, _ = make_pinned()
        self.held = {"repository": "PerishLab/example", "marker": "v1.0.0", "wharf": "b" * 40, "run": "7", "attempt": "2", "planned": "1", "source": "/original"}
        identity = {"repository": self.held["repository"], "marker": self.held["marker"], "commit": "a" * 40, "tree": "c" * 40}
        self.expected = {"schema": "wharf.release.snapshot/v1", "source": identity, "head": "d" * 40, "tree": "e" * 40, "packages": [], "controls": {"authority": {"producer": f"{PLUMB_VERSION}@{COMMIT}", "depot": self.pinned["generation"]}}, "domain": DOMAIN, "guard": {}}
        self.document = {"context": dict(self.held, **identity, attempt="1", snapshot=self.expected, configuration=self.pinned)}

    def test_execution_passes_pinned_configuration_in_perform_and_admit_and_enforces_env_match(self):
        seen = []
        bucket = Memory()

        @contextmanager
        def prepared(request):
            seen.append(request.configuration)
            yield Path("/verified"), self.expected, lambda: None

        env = {configuration.VARIABLE: canonical.encode(self.pinned).decode()}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(execution.r2, "configured", return_value=bucket), mock.patch.object(execution.plan, "read", return_value=self.document), mock.patch.object(execution.toolchain, "versions", return_value=DOMAIN), mock.patch.object(snapshot, "prepared", prepared), mock.patch.object(execution.parameters, "answer"):
            execution.perform("oci-publish", lambda scoped: scoped["publication"](), self.held)
        self.assertEqual(seen, [self.pinned, self.pinned])

    def test_execution_refuses_mismatched_malformed_or_authority_drifted_configuration(self):
        other, _, _ = make_pinned("v0.91.0", b"level = 2\n")
        for raw in (canonical.encode(other).decode(), "null", "", "{not-json", "{}"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {configuration.VARIABLE: raw}, clear=False), mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value=self.document), mock.patch.object(snapshot, "prepared") as prepared:
                with self.assertRaises(Refusal):
                    execution.perform("binary", mock.Mock(), self.held)
                prepared.assert_not_called()
        drifted = {"context": dict(self.document["context"], configuration=other)}
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value=drifted):
            os.environ.pop(configuration.VARIABLE, None)
            with self.assertRaisesRegex(Refusal, "snapshot authority"):
                execution.perform("binary", mock.Mock(), self.held)
        legacy_doc = {"context": {key: value for key, value in self.document["context"].items() if key != "configuration"}}
        with mock.patch.dict(os.environ, {configuration.VARIABLE: canonical.encode(self.pinned).decode()}, clear=False), mock.patch.object(execution.r2, "configured"), mock.patch.object(execution.plan, "read", return_value=legacy_doc):
            with self.assertRaisesRegex(Refusal, "differs from the verified plan"):
                execution.perform("binary", mock.Mock(), self.held)
        for empty in ("null", ""):
            with self.subTest(legacy_env=empty), mock.patch.dict(os.environ, {configuration.VARIABLE: empty}, clear=False):
                self.assertIsNone(execution.pinned_configuration(legacy_doc, self.expected))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(configuration.VARIABLE, None)
            self.assertEqual(execution.pinned_configuration(self.document, self.expected), self.pinned)

    def test_plan_record_stores_configuration_outside_snapshot_and_emits_canonical_output(self):
        for pinned in (self.pinned, None):
            with self.subTest(decoupled=pinned is not None), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                (root / "plumb.toml").write_text("[release]\n")
                bucket = Memory()
                held = dict(repository="PerishLab/demo", marker="v1.2.3", commit="a" * 40, tree="b" * 40, wharf="c" * 40, run="7", attempt="1", source=directory, steps=json.dumps({name: {"outputs": {"decision": "skip", "presence": "none"}} for name in plan.MEDIA}))
                identity = {name: held[name] for name in plan.IDENTITY}
                verified = dict(schema="wharf.release.snapshot/v1", source=identity, head="d" * 40, tree=held["tree"], packages=[], controls={"authority": {"producer": f"{PLUMB_VERSION}@{COMMIT}", "depot": self.pinned["generation"]}}, domain=DOMAIN, guard={})

                @contextmanager
                def prepared(request, selected=pinned):
                    if selected is not None and request.captured is not None:
                        request.captured["configuration"] = selected
                    yield root, verified, lambda: None

                for target, name, value in ((plan.snapshot, "prepared", prepared), (plan.toolchain, "versions", lambda: DOMAIN), (plan.r2, "configured", lambda: bucket), (plan.basis, "carried", lambda source: False), (plan.trial, "capture", lambda source, identity: None), (plan.node, "carried", lambda source: False), (plan.cfworker, "workers", lambda source: []), (plan.preflight, "planning", lambda *args: None), (plan.material, "capture", lambda source, verified: {"schema": "wharf.release.material/v1", "snapshot": "0" * 64, "files": []})):
                    stack.enter_context(mock.patch.object(target, name, value))
                stack.enter_context(mock.patch.object(plan.parameters, "answer"))
                result = plan.record(held)
                stored = json.loads(bucket.get(result["plan"]))
                self.assertEqual(set(stored["context"]["snapshot"]), {"schema", "source", "head", "tree", "packages", "controls", "domain", "guard"})
                if pinned is None:
                    self.assertNotIn("configuration", stored["context"])
                    self.assertEqual(result["decided"]["configuration"], "null")
                else:
                    self.assertEqual(stored["context"]["configuration"], pinned)
                    self.assertEqual(result["decided"]["configuration"], canonical.encode(pinned).decode())

    def test_guard_checked_and_seal_support_both_legacy_and_decoupled_shapes(self):
        rel = release.Release("PerishLab/plumb", PLUMB_VERSION, COMMIT, "b" * 40)
        producer = f"{PLUMB_VERSION}@{COMMIT}"
        self.assertEqual(guard.checked(rel, {"producer": producer, "depot": LEGACY_DEPOT}), {"producer": producer, "depot": LEGACY_DEPOT})
        self.assertEqual(guard.checked(rel, {"producer": producer}), {"producer": producer})
        _, raw = release.seal(rel, "plumb", ({}, {}, guard.checked(rel, {"producer": producer})), "https://releases.plumb.perish.uk")
        self.assertEqual(json.loads(raw)["guard"], {"producer": producer})
        for bad in ({}, {"depot": LEGACY_DEPOT}, {"producer": producer, "depot": "Z" * 64}, {"producer": producer, "extra": "1"}):
            with self.subTest(bad=bad), self.assertRaises(Refusal):
                guard.checked(rel, bad)
