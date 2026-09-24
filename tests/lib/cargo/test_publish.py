import gzip
import io
import json
import tarfile
import unittest
from pathlib import Path

from lib.cargo import publish, registry, version
from lib.refusal import Conflict, Refusal
from tests.lib.cargo.test_basis import Repository

LOCATION = "https://cargo.perish.uk/"
CONFIG = {"dl": LOCATION + "crates/{crate}/{version}/{sha256-checksum}.crate"}


def crate(name, vers, body=""):
    manifest = f'[package]\nname = "{name}"\nversion = "{vers}"\n{body}'.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(f"{name}-{vers}/Cargo.toml")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return gzip.compress(buffer.getvalue(), mtime=0)


class Bucket:
    def __init__(self):
        self.objects = {"config.json": json.dumps(CONFIG).encode()}
        self.conflicts = 0

    def head(self, key):
        return str(hash(self.objects[key])) if key in self.objects else None

    def get(self, key):
        return self.objects[key]

    def create(self, key, body, headers=None):
        if key in self.objects:
            raise Conflict(key)
        self.objects[key] = body

    def conditional(self, operation):
        held = operation.headers.get("If-Match")
        if self.conflicts or held != self.head(operation.key):
            self.conflicts = max(self.conflicts - 1, 0)
            raise Conflict(operation.key)
        self.objects[operation.key] = operation.body


class Registry:
    def __init__(self):
        self.bucket = Bucket()
        self.runs = []
        self.stores = []

    def reader(self, url):
        held = self.bucket.objects.get(url.removeprefix(LOCATION))
        return held.decode() if held is not None else ""

    def run(self, argv, cwd, env=None):
        self.runs.append((argv, env))
        package = argv[argv.index("--package") + 1]
        target = Path(argv[argv.index("--target-dir") + 1]) / "package"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{package}-1.2.3-beta.1.crate").write_bytes(crate(package, "1.2.3-beta.1"))
        return ""

    def store(self, name, role):
        self.stores.append((name, role))
        return self.bucket

    def tools(self):
        return publish.Tools(run=self.run, reader=self.reader, sleep=lambda seconds: None, store=self.store)

    def lines(self, package):
        return [json.loads(line) for line in self.reader(LOCATION + registry.entry(package)).splitlines()]


def publishing(repository):
    repository.write(".cargo/config.toml", f'[registries.perish]\nindex = "sparse+{LOCATION}"\n')
    repository.edit("crates/lib/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = ["perish"]')
    repository.edit("crates/cli/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = ["perish"]')
    repository.edit("crates/other/Cargo.toml", 'version.workspace = true', 'version.workspace = true\npublish = false')
    repository.commit()


class Publish(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        publishing(self.repository)
        self.registry = Registry()

    def test_orders_by_workspace_dependency(self):
        self.assertEqual(publish.publishable(self.repository.root), [("demo", "perish"), ("demo-cli", "perish")])

    def test_orders_by_a_dependency_the_workspace_pins(self):
        self.repository.edit("Cargo.toml", '[workspace.package]', '[workspace.dependencies]\ndemo = { path = "crates/lib", version = "=0.0.0" }\n\n[workspace.package]')
        self.repository.edit("crates/cli/Cargo.toml", 'demo = { path = "../lib", version = "=0.0.0" }', 'demo.workspace = true')
        self.repository.commit()
        self.assertEqual(publish.publishable(self.repository.root), [("demo", "perish"), ("demo-cli", "perish")])

    def test_publishes_pending_packages_in_order(self):
        self.registry.bucket.objects["de/mo/demo"] = (json.dumps({"name": "demo", "vers": "1.2.3-beta.1"}) + "\n").encode()
        result = publish.publish(self.repository.root, "1.2.3-beta.1", self.registry.tools())
        self.assertEqual([item["state"] for item in result["packages"]], ["already-published", "published"])
        argv, env = self.registry.runs[0]
        self.assertEqual(argv[:2], ["cargo", "package"])
        self.assertEqual(argv[argv.index("--package") + 1], "demo-cli")
        self.assertEqual(env["RUSTUP_TOOLCHAIN"], "1.96.1")
        self.assertEqual(self.registry.stores, [("perish-cargo", "CARGO")])
        [line] = self.registry.lines("demo-cli")
        self.assertEqual((line["name"], line["vers"], line["yanked"]), ("demo-cli", "1.2.3-beta.1", False))
        self.assertEqual(self.registry.bucket.objects[f"crates/demo-cli/1.2.3-beta.1/{line['cksum']}.crate"], crate("demo-cli", "1.2.3-beta.1"))

    def test_appends_after_a_concurrent_change(self):
        self.registry.bucket.conflicts = 2
        publish.publish(self.repository.root, "1.2.3-beta.1", self.registry.tools())
        self.assertEqual([line["vers"] for line in self.registry.lines("demo")], ["1.2.3-beta.1"])

    def test_keeps_standing_lines(self):
        self.registry.bucket.objects["de/mo/demo"] = (json.dumps({"name": "demo", "vers": "1.0.0"}) + "\n").encode()
        publish.publish(self.repository.root, "1.2.3-beta.1", self.registry.tools())
        self.assertEqual([line["vers"] for line in self.registry.lines("demo")], ["1.0.0", "1.2.3-beta.1"])

    def test_refuses_different_bytes_under_a_standing_crate(self):
        cksum = __import__("hashlib").sha256(crate("demo", "1.2.3-beta.1")).hexdigest()
        self.registry.bucket.objects[f"crates/demo/1.2.3-beta.1/{cksum}.crate"] = b"other"
        with self.assertRaises(Refusal):
            publish.publish(self.repository.root, "1.2.3-beta.1", self.registry.tools())

    def test_refuses_unknown_registry(self):
        self.repository.write(".cargo/config.toml", '[registries.perish]\nindex = "sparse+https://elsewhere/"\n')
        self.repository.commit()
        with self.assertRaises(Refusal):
            publish.pending(self.repository.root, "1.2.3-beta.1", self.registry.reader)


class Inject(unittest.TestCase):
    def test_binds_every_unversioned_declaration(self):
        repository = Repository()
        self.assertEqual(version.inject(repository.root, "1.2.3-beta.1")["version"], "1.2.3-beta.1")
        self.assertIn('version = "=1.2.3-beta.1"', (repository.root / "crates/cli/Cargo.toml").read_text())
        self.assertIn('version = "1.2.3-beta.1"', (repository.root / "Cargo.toml").read_text())
        self.assertNotIn("0.0.0", (repository.root / "Cargo.lock").read_text())

    def test_binds_the_members_pinned_in_workspace_dependencies(self):
        repository = Repository()
        repository.edit("Cargo.toml", '[workspace.package]', '[workspace.dependencies]\ndemo = { path = "crates/lib", version = "=0.0.0" }\n\n[workspace.package]')
        repository.edit("crates/cli/Cargo.toml", 'demo = { path = "../lib", version = "=0.0.0" }', 'demo.workspace = true')
        repository.commit()
        version.inject(repository.root, "1.2.3-beta.1")
        self.assertIn('version = "=1.2.3-beta.1"', (repository.root / "Cargo.toml").read_text())

    def test_marker_is_required(self):
        with self.assertRaises(Refusal):
            version.marker("latest")
        self.assertEqual(version.marker("v1.2.3-beta.1"), "1.2.3-beta.1")
