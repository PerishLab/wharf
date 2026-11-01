import copy
import json
import tempfile
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind import control
from lib.process import git
from lib.store import plan, r2
from scripts import channel
from tests.lib.store.memory import Memory


class World:
    def __init__(self, test, attempt="1"):
        self.root = Path(test.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "plumb.toml").write_text('[release]\nbinaries = ["demo"]\ntargets = ["x86_64-unknown-linux-gnu"]\n')
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "source")
        self.source = dict(repository="PerishLab/demo", marker="v1.2.3", commit=git(self.root, "rev-parse", "HEAD"), tree=git(self.root, "rev-parse", "HEAD^{tree}"))
        self.held = dict(self.source, wharf="a" * 40, run="7", attempt=attempt, planned="1", source=str(self.root))
        snapshot = dict(schema="wharf.release.snapshot/v1", source=self.source, head=self.source["commit"], tree=self.source["tree"], packages={}, controls={}, domain={}, guard={})
        self.context = dict(self.source, wharf=self.held["wharf"], run="7", attempt="1", snapshot=snapshot)
        self.document = plan.document(self.context, {"channel": {"decision": "run"}}, {})
        self.workload = Memory()
        plan.reserve(self.workload, self.document)
        self.save()
        self.bucket = Memory()
        self.seal = dict(schema=1, product="demo", channel="stable", releaseVersion="v1.2.3", commit=self.source["commit"], artifacts={"linux-x64": {}}, managers={"unix": {}})
        self.save_seal()
        self.repository = dict(id=1374358737, full_name="PerishLab/wharf", default_branch="main", owner={"id": 330236468})
        self.branch = dict(name="main", protected=True)
        self.runs = {number: self.run(number) for number in {"1", attempt}}
        self.jobs = {number: [self.job(number, "[plan] entries", 1)] for number in self.runs}
        self.jobs[attempt].append(self.job(attempt, "[point] channel", 2))
        self.calls = []
        test.enterContext(mock.patch.dict("os.environ", {"GITHUB_JOB": "channel"}))
        test.enterContext(mock.patch.object(control, "api", side_effect=self.api))
        test.enterContext(mock.patch.object(r2, "configured", return_value=self.workload))
        test.enterContext(mock.patch.object(r2, "writer", return_value=self.bucket))

    def save(self):
        self.workload.objects[plan.location(self.context)] = canonical.encode(self.document)

    def save_seal(self):
        self.bucket.objects[channel.release.sealed(channel.released(self.held))] = json.dumps(self.seal).encode()

    def run(self, attempt):
        return dict(id=7, run_attempt=int(attempt), head_sha=self.held["wharf"], repository=self.repository, head_repository=self.repository, path=".github/workflows/ship.yml", event="workflow_dispatch", head_branch="main", status="in_progress", conclusion=None)

    def job(self, attempt, name, identity):
        active = name == "[point] channel"
        return dict(id=identity, run_id=7, run_attempt=int(attempt), head_sha=self.held["wharf"], head_branch="main", name=name, status="in_progress" if active else "completed", conclusion=None if active else "success", started_at="2026-11-01T11:00:00Z", steps=[dict(name="[record] plan", status="completed", conclusion="success")])

    def api(self, path):
        self.calls.append(path)
        if not path:
            return copy.deepcopy(self.repository)
        if path == "branches/main":
            return copy.deepcopy(self.branch)
        parts = path.split("/")
        attempt = parts[4]
        if len(parts) == 5:
            return copy.deepcopy(self.runs[attempt])
        held = self.jobs[attempt]
        return copy.deepcopy(dict(total_count=len(held), jobs=held))
