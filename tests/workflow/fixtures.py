import copy
import json

from lib.content.lane import codec
from lib.store.lane import documents, registration, state
from tests.lib.preview.test_admission import CONTEXT, Provider
from tests.lib.preview.test_entry import PLATFORM


def registered(generation="c" * 64, lane="preview"):
    return {"schema": "wharf.lane.registration/v1", "declaration": {
        "target": {"repository": "PerishLab/crest", "app": "crest-review", "lane": lane},
        "adaptor": "static", "capabilities": ["inspect", "deploy", "dispose"],
        "authorities": {"authorization": "ensign", "publication": "wharf"}},
        "mapping": {"provider": "cfworker", "access": "public", "account": "a" * 32, "resource": "crest-review"},
        "generation": generation, "enabled": True}


def request(operation="apply", target=None):
    target = registered() if target is None else target
    current = settled(target) if operation == "dispose" else state.initial(target)
    return {"schema": "wharf.lane.dispatch/v1", "target": copy.deepcopy(target["declaration"]["target"]),
            "operation": operation, "request": "f" * 64, "revision": current["revision"], "expected": current["observed"],
            "source": {"commit": "d" * 40, "tree": "e" * 40, "declaration": target["generation"]} if operation == "apply" else None,
            "registration": registration.digest(target), "caller": "alice",
            "workflow": "PerishLab/wharf/.github/workflows/lane.yml@" + CONTEXT["commit"]}


def settled(target):
    artifact = {"digest": "1" * 64, "source": {"repository": "PerishLab/crest", "commit": "d" * 40, "tree": "e" * 40},
                "build": {"inputs": "2" * 64, "world": "3" * 64}}
    request = {"context": {"requested": target["declaration"]["target"], "request": "7" * 64, "revision": 1, "adaptor": "static"},
               "intent": {"action": "deploy", "artifact": artifact}, "expected": {"state": "absent"}}
    assessed = {"request": codec.digest("request", request), "capabilities": target["declaration"]["capabilities"],
                "conditions": {"authorization": {"state": "granted", "evidence": {"authority": "ensign", "digest": "8" * 64}},
                               "writer": "idle", "observed": {"state": "absent"}}}
    current = state.started(state.initial(target), {"request": request, "assessment": assessed}, target)
    return state.finished(current, {"request": request, "assessment": assessed,
                                   "outcome": {"outcome": "applied", "material": {"state": "present", "artifact": artifact}, "writer": "idle"}}, target)


def provider():
    result = Provider()
    result.observed["path"] = ".github/workflows/lane.yml"
    return result


def platform():
    return dict(PLATFORM, GITHUB_WORKFLOW_REF="PerishLab/wharf/.github/workflows/lane.yml@refs/heads/main")


class Store:
    def __init__(self, target, current=None):
        self.target, self.current = copy.deepcopy(target), copy.deepcopy(current)
        self.objects, self.calls = {}, []

    def snapshot(self, key):
        self.calls.append(("snapshot", key))
        root = documents.location(self.target["declaration"]["target"])
        if key == root + "/registration.json":
            return documents.encode(self.target), '"registration"'
        if key == root + "/current.json":
            return (None, None) if self.current is None else (documents.encode(self.current), '"current"')
        raise AssertionError(key)

    def create(self, key, body):
        self.calls.append(("create", key))
        if key in self.objects:
            from lib.refusal import Conflict
            raise Conflict("immutable")
        self.objects[key] = body

    def swap(self, key, body, etag):
        self.calls.append(("swap", key))
        expected = None if self.current is None else '"current"'
        if etag != expected:
            from lib.refusal import Conflict
            raise Conflict("state moved")
        self.current = documents.decode(body)

    def get(self, key):
        self.calls.append(("get", key))
        return self.objects[key]


def encoded(value):
    return json.dumps(value, separators=(",", ":"))
