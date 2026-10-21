import copy

from lib.content.lane import codec, shape
from lib.process.confirmation import history, store
from lib.process.lane import execution
from lib.store.lane import documents, public
from tests.confirmation.fixtures import bundle
from tests.workflow import fixtures
from tests.workflow.test_execution import prepared


class Snapshots:
    def __init__(self, bucket, intent):
        self.bucket, self.keys, self.calls = bucket, store.keys(intent), []
        self.etags = {name: '"' + name + '"' for name in self.keys}

    def snapshot(self, key):
        self.calls.append(key)
        name = next(name for name, value in self.keys.items() if value == key)
        if name == "registration":
            body = documents.encode(self.bucket.target)
        elif name == "current":
            body = documents.encode(self.bucket.current)
        else:
            body = self.bucket.objects.get(key)
        return (None, None) if body is None else (body, self.etags[name])


class Control:
    def __init__(self, old, inventory):
        self.runs = {123: copy.deepcopy(old.observed), 456: dict(copy.deepcopy(old.observed), id=456, run_attempt=1,
                     head_sha="2" * 40, status="in_progress", conclusion=None,
                     actor={"id": 30, "login": "carol"}, triggering_actor={"id": 31, "login": "dave"})}
        self.inventory, self.calls = copy.deepcopy(inventory), []
        self.source = {"sha": history.COMMIT, "tree": {"sha": history.TREE}}
        self.control = {"id": 10, "full_name": "PerishLab/wharf", "archived": False, "default_branch": "main"}

    def repository(self, name):
        self.calls.append(("repository", name))
        if name != "PerishLab/wharf":
            raise AssertionError(name)
        return copy.deepcopy(self.control)

    def run(self, identifier):
        self.calls.append(("run", identifier))
        return copy.deepcopy(self.runs[identifier])

    def jobs(self, identifier, attempt):
        self.calls.append(("jobs", identifier, attempt))
        if (identifier, attempt) != (123, 1):
            raise AssertionError((identifier, attempt))
        return copy.deepcopy(self.inventory)

    def commit(self, repository, commit):
        self.calls.append(("commit", repository, commit))
        if (repository, commit) != ("PerishLab/wharf", history.COMMIT):
            raise AssertionError((repository, commit))
        return copy.deepcopy(self.source)


def confirmed():
    intent, bucket, held = prepared()
    _, _, old, inventory = bundle()
    intent["workflow"] = "PerishLab/wharf/.github/workflows/lane.yml@" + history.COMMIT
    held["custody"]["commit"] = history.COMMIT
    bucket.objects[execution.location(intent)] = documents.encode(held["custody"])
    old.observed["head_sha"] = history.COMMIT
    for job in inventory["jobs"]:
        job["head_sha"] = history.COMMIT
    record = {"schema": 2, "entry": {"kind": "operation", "value": {"request": held["request"], "assessment": held["assessment"],
              "outcome": {"outcome": "unknown", "reason": "unverified"}}}}
    body = codec.encode("record", record)
    reader = public.prepare({"authority": "wharf", "origin": "https://evidence.example.test"})
    reference = reader.reference(codec.digest("record", record))
    current = {"run": 456, "attempt": 1, "commit": "2" * 40}
    pins = {"schema": "wharf.lane.confirmation-pins/v1", "dispatch": intent, "original": reference,
            "context": {"run": 123, "attempt": 1, "commit": history.COMMIT},
            "actors": {key: old.observed[key] for key in ("actor", "triggering_actor")},
            "observer": {"context": current, "actors": {"actor": {"id": 30, "login": "carol"}, "triggering_actor": {"id": 31, "login": "dave"}}},
            "current": {"digest": shape.digest(bucket.current), "etag": '"current"'},
            "publication": {"authority": "wharf", "origin": "https://evidence.example.test"}}
    selector = {"schema": "wharf.lane.confirmation-request/v1", "target": intent["target"], "request": intent["request"], "original": reference}
    product = fixtures.provider()
    product.permissions = {user: {"permission": "write", "user": {"id": identifier, "login": user, "permissions": {"push": True}}}
                           for user, identifier in (("carol", 30), ("dave", 31))}
    product.calls.clear()
    return selector, pins, (Snapshots(bucket, intent), reader), (current, Control(old, inventory), product), body
