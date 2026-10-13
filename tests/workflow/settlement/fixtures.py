import contextlib
from types import SimpleNamespace
from unittest import mock

from lib.content.lane import codec
from lib.process.lane import execution, publication, settlement
from lib.store.lane import public
from tests.lib.preview.test_admission import CONTEXT
from tests.workflow import fixtures
from tests.workflow.publication import fixtures as evidence


def bundle(action="apply", outcome="applied"):
    held = evidence.bundle(action, outcome)
    record = {"schema": 2, "entry": {"kind": "operation", "value": held["operation"]}}
    reader = public.prepare({"authority": "wharf", "origin": held["env"]["WHARF_LANE_PUBLICATION_ORIGIN"]})
    reference = reader.reference(codec.digest("record", record))
    held["bucket"].objects[public.location(reference)] = codec.encode("record", record)
    held["held"] = {key: held["held"][key] for key in ("request", "prepared")}
    held["held"]["reference"] = fixtures.encoded(reference)
    held["env"]["GITHUB_JOB"] = "state"
    held["env"].update(WHARF_LANE_READER_ENDPOINT="https://state.example.test", WHARF_LANE_READER_BUCKET="lane-state")
    jobs = [{"id": index + 1, "name": name, "run_id": CONTEXT["run"], "run_attempt": CONTEXT["attempt"],
             "head_sha": CONTEXT["commit"], "status": "completed", "conclusion": conclusion}
            for index, (name, conclusion) in enumerate(execution.producer_names(action).items())]
    jobs.append({"id": 99, "name": "[record] evidence lane / [record] state lane", "run_id": CONTEXT["run"],
                 "run_attempt": CONTEXT["attempt"], "head_sha": CONTEXT["commit"], "status": "in_progress", "conclusion": None})
    held["jobs"] = {"total_count": len(jobs), "jobs": jobs}
    held["api"] = fixtures.provider()
    held["api"].jobs = mock.Mock(side_effect=lambda run, attempt: held["jobs"])
    return held


@contextlib.contextmanager
def runtime(held):
    def observed(intent, value, environ):
        return evidence.observe(dict(held, control=held["api"]), intent, value, environ)

    def quiescent(intent, environ):
        return execution.native_jobs(intent, CONTEXT, held["api"])

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(execution, "observe", side_effect=observed))
        stack.enter_context(mock.patch.object(execution, "quiescence", side_effect=quiescent))
        stack.enter_context(mock.patch.object(settlement, "entry_api", return_value=held["api"]))
        stack.enter_context(mock.patch.object(publication, "configured", return_value=SimpleNamespace(host="state.example.test", name="lane-state", credentials=None)))
        stack.enter_context(mock.patch.object(settlement, "Bucket", return_value=held["store"]))
        stack.enter_context(mock.patch.object(public.http.client, "HTTPSConnection", side_effect=lambda host, timeout: evidence.Connection(held)))
        yield
