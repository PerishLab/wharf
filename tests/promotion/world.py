import contextlib
import copy
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from lib.identity.bind import producer
from lib.process.promotion import control, operation, reader, subject
from tests.lib import process as fixtures
from tests.scripts.test_distribution.binary_world import World as Binary


class World:
    def __init__(self):
        self.reference = Binary()
        with self.reference.wired():
            producer.produce(self.reference.held)
        self.world = Binary("v1.2.3", self.reference)
        self.world.observer = dict(self.world.publisher)
        self.native = "e" * 40
        self.message = "v1.2.3\n\npromotes v1.2.3-rc.1\n"
        self.jobs = [fixtures.job(control.JOB, 8, 30, started_at=self.world.started)]
        self.temporary = tempfile.TemporaryDirectory()
        self.held = dict(self.world.held, observer=json.dumps(self.world.observer), planned="1", source=str(Path(self.temporary.name) / "promotion"))
        self.passed = {"source": self.reference.source, "declaration": {"manifest": "d" * 64}, "products": [{"name": "demo", "version": "v1.2.3-rc.1", "commit": "b" * 40, "archive": self.reference.archive["sha256"], "seal": self.reference.seal_ref["sha256"]}]}
        self.ref = self.world.reference(self.reference.root + "/windtun/attempts/7-1/pass.json", json.dumps(self.passed).encode(), "application/json")
        self.qualified = {"reader": {"producer": "v0.118.0@" + "f" * 40, "depot": "0" * 64}, "report": {"evidence": {"source": self.reference.source, "credential": self.ref, "intent": {}, "tag": {}, "observer": {"run": "7"}, "world": {"image": "actual"}, "final_requirements": {"requirements": {"policy": "current"}, "commit": "a" * 40}}}}
        for name in list(self.world.documents):
            if "/v1/releases/stable/" in name:
                del self.world.documents[name]

    def close(self):
        self.temporary.cleanup()

    def api(self, path):
        if "/jobs?" in path:
            return fixtures.jobs(copy.deepcopy(self.jobs))
        return self.world.api(path)

    def tag(self, path):
        if "/git/ref/tags/" in path:
            return fixtures.ref("tag", "v1.2.3", self.native, repo="PerishLab/demo")
        return fixtures.annotation("v1.2.3", self.native, self.world.source["commit"], repo="PerishLab/demo", message=self.message)

    @contextlib.contextmanager
    def wired(self):
        with self.world.wired(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(control.control, "api", side_effect=self.api))
            stack.enter_context(mock.patch.dict(os.environ, {"GITHUB_JOB": control.ROLE}))
            stack.enter_context(mock.patch.object(subject, "PILOT", "PerishLab/demo"))
            stack.enter_context(mock.patch.object(subject, "api", side_effect=self.tag))
            stack.enter_context(mock.patch.object(reader, "check", side_effect=lambda *args: copy.deepcopy(self.qualified)))
            stack.enter_context(mock.patch.object(operation.parameters, "answer"))
            yield
