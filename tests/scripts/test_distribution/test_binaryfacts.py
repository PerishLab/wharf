import copy
import gzip
import io
import json
import struct
import tarfile
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from lib.content import canonical, implementation
from lib.identity import bind
from lib.identity.bind import artifacts, control, inventory, producer, storage
from lib.identity.bind.spec import CARGO, TARGET
from lib.media import archive
from lib.refusal import Refusal
from lib.store import workload
from tests.scripts.test_distribution.binary_world import World


class Content(unittest.TestCase):
    def test_same_key_is_not_sufficient_when_record_or_bytes_change(self):
        world = World()
        implementation_held = implementation.resourced(*CARGO)
        storage.read(world.blob, world.build_key, implementation_held)
        prefix = workload.prefix(world.build_key)
        original = dict(world.blob.objects)
        for suffix, body in (("basis.json", b'{}'), ("record.json", b'{"hash_version":1}'), ("blobs/demo-" + TARGET, b"changed")):
            world.blob.objects = dict(original)
            world.blob.objects[prefix + suffix] = body
            with self.subTest(suffix=suffix), self.assertRaises(Refusal):
                storage.read(world.blob, world.build_key, implementation_held)
        world.blob.objects = original
        with self.assertRaises(Refusal):
            storage.read(world.blob, world.build_key, {})

    def test_record_paths_are_checked_before_restoration(self):
        world = World()
        name = workload.prefix(world.build_key) + "record.json"
        record = json.loads(world.blob.get(name))
        record["files"]["../../outside"] = {"size": 1, "sha256": "a" * 64}
        world.blob.objects[name] = canonical.encode(record)
        with self.assertRaises(Refusal):
            storage.read(world.blob, world.build_key, implementation.resourced(*CARGO))

    def test_exact_unbinding_and_complete_receipts(self):
        world = World()
        with world.wired():
            public = control.public(world.held)
            facts = producer.verified(world.blob, public, world.plan)
        self.assertEqual(set(facts["binaries"]), {"demo", "demo-api"})
        self.assertEqual(set(facts["files"]), {"demo"})
        loaded = facts["binding"]
        for mutation in (dict(loaded, receipt=dict(loaded["receipt"], binaries=[])), dict(loaded, basis={}), dict(loaded, files={"receipt.json": b"{}"})):
            with self.subTest(mutation=mutation.keys()), self.assertRaises(Refusal):
                artifacts.binaries(public, world.declaration, facts["build"], mutation)

    def test_wrong_origin_binding_architecture_and_outside_bytes_refuse(self):
        world = World()
        body = world.bound["demo-" + TARGET]
        original, _ = artifacts.unbound(body, world.source, world.build_key)
        self.assertIsNone(bind.inspect(original)[1])
        changes = [body[:-1] + b"X", body[:18] + struct.pack("<H", 183) + body[20:]]
        with world.wired():
            public = control.public(world.held)
            facts = producer.verified(world.blob, public, world.plan)
        for changed in changes:
            loaded = copy.deepcopy(facts["binding"])
            loaded["files"]["demo-" + TARGET] = changed
            with self.subTest(changed=changed[-1:]), self.assertRaises(Refusal):
                artifacts.binaries(public, world.declaration, facts["build"], loaded)
        with self.assertRaises(Refusal):
            artifacts.unbound(body, dict(world.source, marker="v1.2.4"), world.build_key)
        with self.assertRaises(Refusal):
            artifacts.unbound(body, world.source, "f" * 64)

    def test_archive_missing_extra_duplicate_order_and_metadata_refuse(self):
        world = World()
        a = world.bound["demo-" + TARGET]
        for members in ([("demo-api", a)], [("demo", a), ("demo-api", a)], [("demo", a), ("demo", a)], [("../demo", a)]):
            with self.subTest(members=[n for n, _ in members]), self.assertRaises(Refusal):
                artifacts.unpack(archive.tarball(members), ["demo"])
        raw = archive.tarball([("demo", a)])
        for changed in (raw + b"ignored", gzip.compress(gzip.decompress(raw), mtime=10), raw[:-5], b"not an archive"):
            with self.assertRaises(Refusal):
                artifacts.unpack(changed, ["demo"])
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            member = tarfile.TarInfo("demo")
            member.type, member.linkname = tarfile.SYMTYPE, "/outside"
            tar.addfile(member)
        with self.assertRaises(Refusal):
            artifacts.unpack(stream.getvalue(), ["demo"])


class Declaration(unittest.TestCase):
    def test_current_plan_freezes_all_and_installed_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "plumb.toml").write_text('[release]\nbinaries=["demo","demo-api"]\ntargets=["x86_64-unknown-linux-gnu"]\n[release.binary.demo-api]\ninstall=false\n')
            value = inventory.capture(directory)
        self.assertEqual(value["built"], ["demo", "demo-api"])
        self.assertEqual(value["installed"], ["demo"])
        inventory.validate(value)

    def test_historical_manifest_requires_exact_recorded_digest(self):
        world = World()
        raw = b'[release]\nbinaries=["demo"]\ntargets=["x86_64-unknown-linux-gnu"]\n'
        public = {"source": world.source, "distribution": {}}
        from lib.identity.bind.spec import sha
        trial = {"source": world.source, "manifest": sha(raw)}
        public["distribution"]["trial"] = trial
        document = {"context": {"trial": trial}}
        with mock.patch.object(inventory.release, "fetch", return_value=raw):
            self.assertEqual(inventory.resolve(public, document)["installed"], ["demo"])
            with self.assertRaises(Refusal):
                inventory.resolve(public, {"context": {}})
        with mock.patch.object(inventory.release, "fetch", return_value=raw + b"changed"), self.assertRaises(Refusal):
            inventory.resolve(public, document)
