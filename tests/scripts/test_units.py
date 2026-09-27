import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.store import plan
from scripts import ship
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/santi", "marker": "v0.1.0-rc.2", "wharf": "c" * 40, "run": "9", "attempt": "1"}
LINUX = "x86_64-unknown-linux-gnu"
SANTI = f'[release]\nbinaries = ["santi", "santi-api"]\ntargets = ["{LINUX}"]\n[release.binary.santi-api]\ninstall = false\n'
DEB = '[release.deb]\nbinary = "santi-api"\nroot = "packaging/deb"\n'


class Seeded(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.source = Path(tempfile.mkdtemp())
        self.held = dict(CONTEXT, planned="1", source=str(self.source), target=LINUX)

    def declare(self, body):
        (self.source / "plumb.toml").write_text(body)

    def recorded(self, key, files):
        produced = Path(tempfile.mkdtemp()) / "produced"
        produced.mkdir()
        for name, body in files.items():
            (produced / name).write_bytes(body)
        ship.workload.publish(self.bucket, key, ship.workload.Produced(produced, {}, {}))
        return key

    def planned(self, entries):
        plan.record(self.bucket, dict(CONTEXT, commit="a" * 40, tree="b" * 40), entries, {})

    def key(self, basis, modules):
        return ship.workload.key(basis, ship.implementation.resourced(*modules))

    def configured(self):
        return mock.patch.object(ship.r2, "configured", return_value=self.bucket)


class Units(Seeded):
    def test_smoke_runs_every_bound_executable_against_the_marker(self):
        bound = self.recorded("b" * 64, {f"santi-{LINUX}": b"a", f"santi-api-{LINUX}": b"b", "receipt.json": b"{}"})
        key = self.key({"entry": {"kind": "binary-smoke", "binary": bound}}, (["lib.identity.smoke"], []))
        self.planned({"bind-linux": {"key": bound, "decision": "skip"}, "smoke-linux": {"key": key, "decision": "run"}})
        seen = {}

        def smoked(artifacts, output, marker):
            seen.update(names=[artifact.name for artifact in artifacts], marker=marker)
            Path(output).mkdir(parents=True)
            Path(output).joinpath("receipt.json").write_text("{}")

        with self.configured(), mock.patch.object(ship, "smoke", side_effect=smoked):
            self.assertEqual(ship.run_smoke(self.held)["key"], key)
        self.assertEqual(seen, {"names": ["santi", "santi-api"], "marker": "v0.1.0-rc.2"})

    def test_the_deb_is_built_from_the_bound_executable_it_names(self):
        self.declare(SANTI + DEB)
        bound = self.recorded("b" * 64, {f"santi-{LINUX}": b"cli", f"santi-api-{LINUX}": b"server"})
        held_basis = {"entry": {"kind": "deb"}}
        key = self.key(held_basis, ship.DEBIAN)
        self.planned({"bind-linux": {"key": bound, "decision": "skip"}, "deb": {"key": key, "decision": "run"}})
        seen = {}

        def built(request):
            seen.update(binary=Path(request.bound).read_bytes(), product=request.product, version=request.version, declared=request.declared)
            Path(request.output).mkdir(parents=True)
            Path(request.output).joinpath(ship.package.named(request.product)).write_bytes(b"deb")

        with self.configured(), mock.patch.object(ship.package, "basis", return_value=held_basis) as basis, mock.patch.object(ship.package, "build", side_effect=built):
            self.assertEqual(ship.run_deb(self.held)["key"], key)
        self.assertEqual(basis.call_args.args[2:], (bound, "0.1.0~rc.2"))
        self.assertEqual(seen, {"binary": b"server", "product": "santi", "version": "0.1.0~rc.2", "declared": ship.package.Declared("santi-api", "packaging/deb")})

    def test_the_deb_is_verified_from_its_recorded_workload(self):
        self.declare(SANTI + DEB)
        deb = self.recorded("d" * 64, {f"santi-{LINUX}.deb": b"deb"})
        key = self.key({"entry": {"kind": "deb-verify", "deb": deb}}, ship.VERIFYING)
        self.planned({"deb": {"key": deb, "decision": "skip"}, "verify-deb": {"key": key, "decision": "run"}})
        seen = {}

        def verified(check, output):
            seen.update(deb=Path(check.deb).read_bytes(), binary=check.binary, version=check.version, marker=check.marker, depends=check.depends)
            Path(output).mkdir(parents=True)
            Path(output).joinpath("receipt.json").write_text("{}")

        declared = mock.patch.object(ship.package, "depends", return_value=["adduser"]), mock.patch.object(ship.package, "units", return_value={})
        with self.configured(), declared[0], declared[1], mock.patch.object(ship.verify, "verify", side_effect=verified):
            self.assertEqual(ship.run_verify(self.held)["key"], key)
        self.assertEqual(seen, {"deb": b"deb", "binary": "santi-api", "version": "0.1.0~rc.2", "marker": "v0.1.0-rc.2", "depends": ("adduser",)})


class Released(Seeded):
    def release(self, entries):
        self.planned({"bind-linux": {"key": self.recorded("b" * 64, {f"santi-{LINUX}": b"cli", f"santi-api-{LINUX}": b"server"}), "decision": "skip"}, "release": {"decision": "run"}, **entries})
        seen = {}

        def published(release, contents, bucket):
            seen.update(installed=contents.installed, placed={kind: Path(path).read_bytes() for kind, path in contents.placed.items()})
            return {"state": "published"}

        with self.configured(), mock.patch.object(ship.r2, "writer"), mock.patch.object(ship.releasing, "publish", side_effect=published):
            ship.run_release(self.held)
        return seen

    def test_the_seal_carries_the_installed_executables_and_the_verified_deb(self):
        self.declare(SANTI + DEB)
        deb = self.recorded("d" * 64, {f"santi-{LINUX}.deb": b"deb"})
        verified = self.recorded("e" * 64, {"receipt.json": b"{}"})
        seen = self.release({"deb": {"key": deb, "decision": "skip"}, "verify-deb": {"key": verified, "decision": "run"}})
        self.assertEqual(seen, {"installed": {LINUX: ["santi"]}, "placed": {"deb": b"deb"}})

    def test_a_product_without_a_deb_places_nothing(self):
        self.declare(SANTI)
        self.assertEqual(self.release({})["placed"], {})

    def test_a_declared_deb_the_plan_did_not_build_refuses(self):
        self.declare(SANTI + DEB)
        with self.assertRaisesRegex(ship.Refusal, "did not build and verify"):
            self.release({})

    def test_a_deb_with_no_recorded_verification_refuses(self):
        self.declare(SANTI + DEB)
        deb = self.recorded("d" * 64, {f"santi-{LINUX}.deb": b"deb"})
        with self.assertRaisesRegex(ship.Refusal, "no recorded verification"):
            self.release({"deb": {"key": deb, "decision": "skip"}, "verify-deb": {"key": "e" * 64, "decision": "run"}})
        self.assertEqual(json.loads(self.bucket.get(f"workload/1/{deb}/record.json"))["key"], deb)
