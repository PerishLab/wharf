import contextlib
import json
import os
import struct
from unittest import mock

from lib.cargo.basis import WIDENED
from lib.content import canonical, implementation
from lib.identity import bind
from lib.identity.bind import control, producer
from lib.identity.bind.spec import BIND, CARGO, TARGET, entry, sha
from lib.media import archive, release
from lib.store import plan, workload
from tests.lib.identity.test_bind import executable, region
from tests.scripts.test_distribution.world import Bucket

AUTHORITY = "https://releases.demo.perish.uk"


class World:
    def __init__(self, version="v1.2.3-rc.1", shared=None, commit=None):
        self.blob = shared.blob if shared else Bucket()
        self.public = shared.public if shared else Bucket()
        self.documents = shared.documents if shared else {}
        self.source = {"repository": "PerishLab/demo", "marker": version, "commit": commit or (shared.source["commit"] if shared else "b" * 40), "tree": "c" * 40}
        self.role = control.ROLE
        self.publisher = {"run": "7" if "rc" in version else "8", "attempt": "1", "wharf": "a" * 40}
        self.observer = {"run": "20" if "rc" in version else "21", "attempt": "1", "wharf": "a" * 40}
        self.started = "2026-10-30T10:00:00Z"
        self.declaration = {"manifest": "d" * 64, "target": TARGET, "built": ["demo", "demo-api"], "installed": ["demo"]}
        self.basis = {"entry": {"kind": "cargo-binary", "binaries": self.declaration["built"], "target": TARGET, "runner": "ubuntu-24.04", "packages": ["demo"]}, "toolchain": {"channel": "1.96.1", "profile": "minimal", "components": ["clippy", "rustfmt"], "targets": []}, "manifest": {"Cargo.toml": "e" * 40}, "members": {"crates/demo": "f" * 40}, "lock": [["demo", "0.0.0", "", ""]], "config": {}, "widened": WIDENED}
        self.build_key = workload.key(self.basis, implementation.resourced(*CARGO))
        built = {}
        for name in self.declaration["built"]:
            body = bytearray(executable(region("DEMO")))
            struct.pack_into("<H", body, 18, 62)
            built[name + "-" + TARGET] = bytes(body) + name.encode()
        receipt = {"action": "ship.binary", "product": "demo", "target": TARGET, "binaries": self.declaration["built"], "packages": ["demo"], "toolchain": "1.96.1", "rustc": "rustc 1.96.1", "files": {name: entry(body) for name, body in built.items()}}
        built["receipt.json"] = canonical.encode(receipt)
        self.build_context = dict(shared.build_context) if shared else {"repository": "PerishLab/demo", "marker": version, "run": "7", "attempt": "1", "wharf": "a" * 40}
        self.store((self.build_key, self.basis, self.build_context), built)
        binding_basis = {"entry": {"kind": "binary-identity", "binary": self.build_key}, "identity": self.source}
        self.bind_key = workload.key(binding_basis, implementation.resourced(*BIND))
        binding = {"product": "demo", "marker": version, "commit": self.source["commit"], "digest": canonical.digest(self.source), "workload": self.build_key}
        bound = {name: bind.bind(body, binding) for name, body in built.items() if name != "receipt.json"}
        receipt = {"action": "ship.identity", "binding": binding, "binaries": [{"file": name, "origin": bind.inspect(body)[0], "signature": None, **entry(body)} for name, body in bound.items()]}
        bound["receipt.json"] = canonical.encode(receipt)
        context = dict(self.publisher, repository="PerishLab/demo", marker=version)
        self.store((self.bind_key, binding_basis, context), bound)
        self.bound = bound
        channel = release.channel(version)
        self.root = f"v1/releases/{channel}/{version}"
        packed = archive.tarball([("demo", bound["demo-" + TARGET])])
        self.archive = self.reference("v1/objects/sha256/" + sha(packed) + "/demo.tar.gz", packed, "application/gzip")
        self.seal = {"schema": 1, "product": "demo", "channel": channel, "releaseVersion": version, "commit": self.source["commit"], "artifacts": {"linux-x64": self.archive}}
        self.seal_ref = self.reference(self.root + "/seal.json", canonical.encode(self.seal), release.JSON)
        self.snapshot = {"schema": "wharf.release.snapshot/v1", "source": self.source, "head": "f" * 40, "tree": self.source["tree"], "packages": [], "controls": {}, "domain": {"rust.version": "1.96.1"}, "guard": {}}
        self.distribution = dict(self.source, state="complete", completed=self.publisher, media={"binaries": "published"}, resolution={"binaries": plan.combination(self.snapshot)})
        self.documents[AUTHORITY + "/" + self.root + "/distribution.json"] = canonical.encode(self.distribution)
        context = dict(context, **{name: self.source[name] for name in ("commit", "tree")}, snapshot=self.snapshot, binary_declaration=self.declaration)
        self.plan = plan.document(context, {"binary-linux": {"key": self.build_key, "decision": "run" if not shared else "skip"}, "bind-linux": {"key": self.bind_key, "decision": "run"}}, {})
        self.blob.objects[plan.location(context)] = canonical.encode(self.plan)
        self.held = {"repository": "PerishLab/demo", "marker": version, "run": self.publisher["run"], "observer": json.dumps(self.observer)}

    def store(self, identity, files):
        key, basis, context = identity
        prefix = workload.prefix(key)
        bodies = {"basis.json": canonical.encode(basis), **{"blobs/" + name: body for name, body in files.items()}}
        bodies["record.json"] = canonical.encode({"hash_version": 1, "key": key, "context": context, "files": {name: entry(body) for name, body in files.items()}})
        for name, body in bodies.items():
            if prefix + name in self.blob.objects:
                assert self.blob.get(prefix + name) == body
            self.blob.objects[prefix + name] = body

    def reference(self, key, body, mime):
        self.documents[AUTHORITY + "/" + key] = body
        return release.remote(AUTHORITY, key, body, mime)

    def fetch(self, url):
        key = url.removeprefix(AUTHORITY + "/")
        if url.startswith(AUTHORITY + "/") and key in self.public.objects:
            return self.public.get(key)
        return self.documents[url]

    def api(self, path):
        repository = {"id": 1, "full_name": "PerishLab/wharf", "default_branch": "main"}
        if not path:
            return repository
        run = path.split("/")[2]
        if "/jobs?" in path:
            return {"total_count": 1, "jobs": [{"id": 30, "name": control.ROLES[self.role], "run_id": int(run), "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "main", "status": "in_progress", "conclusion": None, "started_at": self.started}]}
        return {"id": int(run), "run_attempt": 1, "head_sha": "a" * 40, "repository": repository, "head_repository": repository, "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "head_branch": "main", "status": "in_progress" if run == self.observer["run"] else "completed", "conclusion": None if run == self.observer["run"] else "success"}

    @contextlib.contextmanager
    def wired(self):
        with mock.patch.object(release, "fetch", side_effect=self.fetch), mock.patch.object(control, "api", side_effect=self.api), mock.patch.object(producer.r2, "configured", return_value=self.blob), mock.patch.object(producer.r2, "writer", return_value=self.public), mock.patch.object(producer.parameters, "answer"), mock.patch.dict(os.environ, {"GITHUB_JOB": self.role}):
            yield
