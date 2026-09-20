import re

from lib.content import canonical
from lib.refusal import Refusal

SETTLED = {"success", "skipped"}


def location(context):
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", context["repository"]):
        raise Refusal(f"repository {context['repository']!r} is not owner/name")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(-(alpha|beta|rc)\.[1-9]\d*)?", context["marker"]):
        raise Refusal(f"marker {context['marker']!r} is malformed")
    return f"trigger/{context['repository']}/{context['marker']}/{context['run']}-{context['attempt']}.json"


def record(bucket, context, needs):
    jobs = {name: {"result": need.get("result"), "outputs": need.get("outputs", {})} for name, need in sorted(needs.items())}
    state = "complete" if jobs and all(job["result"] in SETTLED for job in jobs.values()) else "incomplete"
    name = location(context)
    bucket.create(name, canonical.encode({"context": context, "jobs": jobs, "state": state}))
    return {"record": name, "state": state}
