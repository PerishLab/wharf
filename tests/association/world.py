import contextlib
import json
import os
from unittest import mock

from lib.content import canonical, implementation
from lib.identity.bind import control as identity
from lib.identity.bind import inventory
from lib.identity.bind.spec import BIND, CARGO, entry
from lib.media import archive, release
from lib.process.association import control, managers, producer, public, signature, spec
from lib.store import plan, workload
from tests.association.fixtures import executable, sign
from tests.scripts.test_distribution.binary_world import AUTHORITY, World as Linux

MANIFEST = b"""[release]
binaries = ["demo", "demo-api"]
targets = ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"]
[release.binary.demo-api]
install = false
"""


class World(Linux):
    def __init__(self, version="v1.2.3-rc.1", shared=None):
        super().__init__(version, shared)
        self.role = "native-proof"
        self.model = inventory.model(MANIFEST)
        self.declaration["manifest"] = self.model["manifest"]
        self.plan["context"]["binary_declaration"] = self.declaration
        self.plan["context"]["native_declaration"] = {"schema": "wharf.binary.declaration/v1", "source": dict(self.source), "model": self.model}
        self.native = {}
        for selected, specification in spec.TARGETS.items():
            self.extend(selected, specification, shared)
        rendered = managers.rendered(self.source, self.model)
        references = {}
        for name, body in rendered["pinned"].items():
            references[name] = self.reference("v1/objects/sha256/" + spec.sha(body) + "/" + release.MANAGERS[name][0], body, release.MANAGERS[name][1])
        self.seal["managers"] = references
        self.seal["generator"] = release.generator(release.Release(self.source["repository"], version, self.source["commit"], self.publisher["wharf"]))
        self.seal_ref = self.reference(self.root + "/seal.json", canonical.encode(self.seal), release.JSON)
        self.distribution["schema"] = 1
        self.distribution["plan"] = plan.reference(self.plan)
        self.documents[AUTHORITY + "/" + self.root + "/distribution.json"] = canonical.encode(self.distribution)
        self.documents[f"https://raw.githubusercontent.com/PerishLab/demo/{self.source['commit']}/plumb.toml"] = MANIFEST
        self.blob.objects[plan.location(self.plan["context"])] = canonical.encode(self.plan)
        if release.channel(version) == "stable":
            rooted = {}
            for name, body in rendered["normalized"].items():
                rooted[name] = self.reference(release.MANAGERS[name][0], body, release.MANAGERS[name][1])
            pointer = {"schema": 1, "product": "demo", "channel": "stable", "releaseVersion": version, "commit": self.source["commit"], "seal": self.seal_ref, "managers": rooted}
            self.documents[AUTHORITY + "/v1/channels/stable.json"] = canonical.encode(pointer)

    def extend(self, selected, specification, shared):
        basis = json.loads(canonical.encode(self.basis))
        basis["entry"].update(target=selected, runner=specification["runner"])
        key = workload.key(basis, implementation.resourced(*CARGO))
        built = {spec.filename(name, selected): executable(name, selected) for name in self.declaration["built"]}
        receipt = {"action": "ship.binary", "product": "demo", "target": selected, "binaries": self.declaration["built"], "packages": ["demo"], "toolchain": "1.96.1", "rustc": "rustc 1.96.1", "files": {name: entry(body) for name, body in built.items()}}
        built["receipt.json"] = canonical.encode(receipt)
        self.store((key, basis, self.build_context), built)
        binding_basis = {"entry": {"kind": "binary-identity", "binary": key}, "identity": self.source}
        bound_key = workload.key(binding_basis, implementation.resourced(*BIND))
        binding = {"product": "demo", "marker": self.source["marker"], "commit": self.source["commit"], "digest": canonical.digest(self.source), "workload": key}
        from lib.identity import bind
        bound = {name: bind.bind(body, binding) for name, body in built.items() if name != "receipt.json"}
        if specification["signature"]:
            bound = {name: sign(body, name) for name, body in bound.items()}
        bound["receipt.json"] = canonical.encode({"action": "ship.identity", "binding": binding, "binaries": [{"file": spec.filename(name, selected), "origin": bind.inspect(bound[spec.filename(name, selected)])[0], "signature": specification["signature"], **entry(bound[spec.filename(name, selected)])} for name in sorted(self.declaration["built"])]})
        self.store((bound_key, binding_basis, dict(self.publisher, repository="PerishLab/demo", marker=self.source["marker"])), bound)
        self.plan["entries"]["binary-" + specification["unit"]] = {"key": key, "decision": "skip" if shared else "run"}
        self.plan["entries"]["bind-" + specification["unit"]] = {"key": bound_key, "decision": "run"}
        body = archive.pack([("demo.exe" if specification["unit"] == "windows" else "demo", bound[spec.filename("demo", selected)])], specification["format"])
        location = "v1/objects/sha256/" + spec.sha(body) + "/demo-" + selected + "." + specification["format"]
        self.seal["artifacts"][specification["key"]] = self.reference(location, body, release.MIMES[specification["format"]])
        self.native[selected] = {"build": key, "binding": bound_key, "built": built, "bound": bound}

    def api(self, path):
        repository = {"id": 1374358737, "node_id": "R_kgDOUesM0Q", "full_name": "PerishLab/wharf", "default_branch": "main"}
        if not path:
            return repository
        if path == "branches/main":
            return {"name": "main", "protected": True}
        run = path.split("/")[2]
        if "/jobs?" in path:
            current = run == self.observer["run"]
            name = (control.ROLES | identity.ROLES)[self.role] if current else "[prove] native binary" if run in ("20", "21") else "[plan] entries"
            step = "[record] plan" if name == "[plan] entries" else name
            return {"total_count": 1, "jobs": [{"id": 30, "name": name, "run_id": int(run), "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "main", "status": "in_progress" if current else "completed", "conclusion": None if current else "success", "started_at": self.started, "steps": [{"name": step, "status": "completed", "conclusion": "success", "started_at": self.started}]}]}
        return {"id": int(run), "run_attempt": 1, "head_sha": "a" * 40, "repository": repository, "head_repository": repository, "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "head_branch": "main", "status": "in_progress" if run == self.observer["run"] else "completed", "conclusion": None if run == self.observer["run"] else "success"}

    @contextlib.contextmanager
    def wired(self):
        with super().wired(), contextlib.ExitStack() as stack:
            for module in (spec, public, managers):
                stack.enter_context(mock.patch.object(module, "fetch", side_effect=lambda url, limit=1048576: self.fetch(url)))
            stack.enter_context(mock.patch.object(signature, "sign", side_effect=sign))
            stack.enter_context(mock.patch.dict(os.environ, {"GITHUB_JOB": self.role}))
            yield
