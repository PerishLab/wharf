import copy

from lib.content import canonical
from lib.content.lane import codec
from lib.process.lane import entry, execution
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.store.memory import Memory
from tests.workflow import fixtures
from tests.workflow.test_execution import prepared


def bundle(action="apply", outcome="applied"):
    intent, store, value = prepared(action)
    observed = execution.admit(intent, value, store, (CONTEXT, fixtures.provider()))
    if outcome == "applied":
        material = {"state": "present", "artifact": value["request"]["intent"]["artifact"]} if action == "apply" else {"state": "absent"}
        actual = {"outcome": outcome, "material": material, "writer": "idle"}
    else:
        actual = {"outcome": outcome, "reason": "unverified" if outcome == "unknown" else "unsupported"}
    operation = codec.read("operation", {"request": value["request"], "assessment": value["assessment"], "outcome": actual})
    supplied = {"execution": canonical.digest(observed), "plan": {"private": "provider-plan-do-not-publish"}, "operation": operation}
    held = {"request": fixtures.encoded(intent), "prepared": fixtures.encoded(value), "provider": fixtures.encoded(supplied)}
    env = dict(fixtures.platform(), GITHUB_JOB="evidence", WHARF_LANE_PUBLICATION_AUTHORITY="wharf",
               WHARF_LANE_PUBLICATION_ORIGIN="https://evidence.example.test")
    store.calls.clear()
    return {"intent": intent, "store": store, "value": value, "held": held, "env": env, "bucket": Memory(), "operation": operation}


class Response:
    def __init__(self, body, status=200, encoding=None):
        self.body, self.status, self.encoding = body, status, encoding

    def getheader(self, name):
        return self.encoding

    def read(self, limit):
        if limit != 65537:
            raise AssertionError(limit)
        return self.body


class Connection:
    def __init__(self, fixture):
        self.fixture, self.key = fixture, None

    def request(self, method, path, headers):
        self.fixture.setdefault("reads", []).append((method, path, headers))
        self.key = path[1:]

    def getresponse(self):
        observed = self.fixture.get("response")
        if observed is not None:
            return observed
        return Response(copy.deepcopy(self.fixture["bucket"].objects.get(self.key, b"missing")))

    def close(self):
        self.fixture["closed"] = True


def observe(fixture, intent, value, environ):
    context = entry.platform(environ)
    return execution.admit(intent, value, fixture["store"], (context, fixture.get("control", fixtures.provider())))
