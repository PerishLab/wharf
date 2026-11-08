import copy
import json
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.identity.bind.spec import entry
from lib.process.confirmation import semantics
from lib.process.lane import execution
from lib.refusal import Conflict
from tests.workflow import fixtures
from tests.workflow.test_execution import prepared


def bundle(action="apply"):
    intent, _, held = prepared(action)
    api = fixtures.provider()
    api.observed.update(status="completed", conclusion="failure")
    actors = {key: {field: api.observed[key][field] for field in ("id", "login")} for key in ("actor", "triggering_actor")}
    custody = held["custody"]
    jobs = [{"id": index + 1, "name": name, "run_id": custody["run"], "run_attempt": custody["attempt"],
             "head_sha": custody["commit"], "head_branch": "main", "status": "completed", "conclusion": conclusion}
            for index, (name, conclusion) in enumerate(execution.producer_names(action).items())]
    jobs.append(dict(jobs[0], id=99, name="[record] evidence lane / [record] state lane", conclusion="failure"))
    inventory = {"total_count": len(jobs), "jobs": jobs}

    def read(run, attempt):
        api.calls.append(("jobs", run, attempt))
        return copy.deepcopy(inventory)

    api.calls.clear()
    api.jobs = mock.Mock(side_effect=read)
    return intent, {"request": held["request"], "custody": custody, "actors": actors}, api, inventory


class Ledger:
    def __init__(self, source, native, authority):
        self.source, self.native, self.authority = source, native, authority
        self.values = {}
        self.store = mock.Mock()
        self.store.snapshot.side_effect = self.snapshot
        self.store.get.side_effect = lambda key: self.values[key]
        self.store.create.side_effect = self.create
        self.store.swap.side_effect = self.swap
        self.root = {"url": "https://releases.plumb.perish.uk/v1/semantics/objects/" + "a" * 64 + ".json", "sha256": "a" * 64, "size": 123}
        self.request = {"schema": "wharf.release.semantics.request/v1", "kind": "history", "root": self.root, "previous": None}

    def snapshot(self, key):
        raw = self.values.get(key)
        return raw, None if raw is None else entry(raw)["sha256"]

    def create(self, key, raw, headers=None):
        if key in self.values:
            raise Conflict("already present")
        self.values[key] = raw

    def swap(self, key, raw, etag):
        if self.snapshot(key)[1] != etag:
            raise Conflict("head moved")
        self.values[key] = raw

    def public(self, url):
        return self.values[url.split(".perish.uk/", 1)[1]]

    def protocol(self, arguments):
        purpose = arguments[arguments.index("--purpose") + 1]
        if purpose == "baseline":
            data = {"origin": {"reference": self.root, "record": {"release": {"marker": self.source["marker"]}}}, "materials": {"world": {"source": self.source}}}
            facts = {"schema": "plumb.release.baseline/v1", "data": data, "authentication": True, "publication": False}
        else:
            request = json.loads(Path(arguments[3]).read_bytes())
            record = {"schema": "wharf.release.semantics.history/v1", "product": "plumb", "repository": {"id": 10, "node_id": "R_plumb", "full_name": "PerishLab/plumb", "default_branch": "main", "owner": {"id": 330236468, "node_id": "O_kgDOE68CNA", "login": "PerishLab"}}, "root": request["root"], "previous": request["previous"], "sequence": 1, "markers": [{"marker": self.source["marker"], "commit": self.source["commit"]}], "native": request["native"]}
            facts = {"schema": "plumb.release.history.materials/v1", "data": record, "authentication": False, "publication": False}
        return 0, {"state": "observed", "facts": facts, "publication": False}

    def inspect(self, value, raw):
        return self.authority, {"state": "observed", "facts": {"schema": "plumb.release.inspection/v1", "data": value, "authentication": False, "publication": False}, "publication": False}

    def patches(self):
        stack = ExitStack()
        for target, name, options in [
            (semantics.capture, "active", {"return_value": self.native}),
            (semantics.preflight, "event", {"return_value": {"semantics": canonical.encode(self.request).decode()}}),
            (semantics.reader, "identity", {"return_value": self.authority}),
            (semantics.reader, "capture", {"side_effect": self.protocol}),
            (semantics.capture, "inspect", {"side_effect": self.inspect}),
            (semantics.r2, "writer", {"return_value": self.store}),
            (semantics.release, "fetch", {"side_effect": self.public}),
            (semantics.parameters, "answer", {}),
        ]:
            stack.enter_context(mock.patch.object(target, name, **options))
        return stack

    def held(self, directory):
        return {"observer": {}, "repository": self.source["repository"], "marker": self.source["marker"], "source": str(Path(directory) / "history")}
