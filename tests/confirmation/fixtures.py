import copy
from unittest import mock

from lib.process.lane import execution
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
