"""Trigger records: the release trail of one distribution run.

One record per run attempt at trigger/<owner>/<repository>/<marker>/<run>-<attempt>.json,
written once after every job has settled. A run is complete only when every job
either succeeded or was skipped by plan.
"""

import re

from wharf.refusal import Refusal
from wharf.store.workload import canonical

SETTLED = {"success", "skipped"}


def location(context):
    repository = context["repository"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise Refusal(f"repository {repository!r} is not owner/name")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(-beta\.\d+)?", context["marker"]):
        raise Refusal(f"marker {context['marker']!r} is malformed")
    return f"trigger/{repository}/{context['marker']}/{context['run']}-{context['attempt']}.json"


def summarize(needs):
    jobs = {}
    for name, need in sorted(needs.items()):
        jobs[name] = {"result": need.get("result"), "outputs": need.get("outputs", {})}
    return jobs


def record(bucket, context, needs):
    jobs = summarize(needs)
    state = "complete" if jobs and all(job["result"] in SETTLED for job in jobs.values()) else "incomplete"
    body = {"context": context, "jobs": jobs, "state": state}
    name = location(context)
    bucket.create(name, canonical(body))
    return {"record": name, "state": state}
