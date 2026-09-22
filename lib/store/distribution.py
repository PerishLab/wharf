import json

from lib.content import canonical, marker
from lib.refusal import Refusal

MEDIA = {"binaries": "release", "npm": "npm", "oci": "oci", "chart": "chart", "cargo": "cargo", "cfworker": "cfworker", "channel": "channel"}
DONE = {"binaries": "published", "npm": "published", "oci": "published", "chart": "published", "cargo": "published", "cfworker": "deployed", "channel": "pointed"}
FAILED = {"failure": "failed", "cancelled": "cancelled", "skipped": "unreached"}
SETTLED = {"success", "skipped"}
IDENTITY = ("repository", "marker", "commit", "tree")


def location(held):
    return f"v1/releases/{marker.channel(held)}/{held}/distribution.json"


def status(name, decision, result):
    if result == "success":
        return DONE[name]
    if result == "skipped" and decision == "skip":
        return "skipped"
    return FAILED.get(result, "absent")


def observed(context, needs):
    decided = needs.get("plan", {}).get("outputs", {})
    media = {name: status(name, decided.get(job, "skip"), needs.get(job, {}).get("result")) for name, job in MEDIA.items()}
    state = "complete" if needs and all(need.get("result") in SETTLED for need in needs.values()) else "incomplete"
    attempt = {"run": context["run"], "attempt": context["attempt"], "wharf": context["wharf"]}
    return {"schema": 1, **{field: context[field] for field in IDENTITY}, "media": media, "state": state, "attempt": attempt, "completed": attempt if state == "complete" else None}


def final(held):
    return held in DONE.values() or held == "skipped"


def merged(standing, fresh):
    if standing is None:
        return fresh
    if (standing["commit"], standing["tree"]) != (fresh["commit"], fresh["tree"]):
        raise Refusal(f"{location(fresh['marker'])} records {standing['commit']}, this run distributes {fresh['commit']}; a marker never moves")
    media = {name: standing["media"].get(name) if final(standing["media"].get(name)) else held for name, held in fresh["media"].items()}
    completed = standing["completed"] or fresh["completed"]
    return dict(fresh, media=media, state="complete" if completed else "incomplete", completed=completed)


def record(bucket, context, needs, reader):
    fresh = observed(context, needs)
    key = location(context["marker"])
    etag = bucket.head(key)
    held = merged(json.loads(bucket.get(key)) if etag else None, fresh)
    body = canonical.encode(held)
    bucket.swap(key, body, etag)
    if reader(key) != body:
        raise Refusal(f"{key} is not served as written")
    return {"distribution": key, "state": held["state"], "media": held["media"]}
