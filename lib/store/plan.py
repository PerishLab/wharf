import json
import re

from lib.content import canonical, marker
from lib.refusal import Refusal

SCHEMA = 1
NAMED = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def named(context):
    if not NAMED.fullmatch(context["repository"]):
        raise Refusal(f"repository {context['repository']!r} is not owner/name")
    if not marker.holds(context["marker"]):
        raise Refusal(f"marker {context['marker']!r} is malformed")
    return f"plan/{context['repository']}/{context['marker']}"


def product(context):
    named(context)
    return context["repository"].split("/", 1)[1]


def location(context):
    return f"{named(context)}/{context['run']}-{context['attempt']}.json"


def standing(context):
    return f"{named(context)}/latest.json"


def document(context, entries, engines):
    return {"schema": SCHEMA, "context": context, "engines": engines, "entries": {name: entries[name] for name in sorted(entries)}}


def read(bucket, context):
    held = json.loads(bucket.get(location(context)))
    if held.get("schema") != SCHEMA:
        raise Refusal(f"{location(context)} is not a schema {SCHEMA} plan")
    return held


def planned(document, name):
    entry = document["entries"].get(name)
    if entry is None:
        raise Refusal(f"the plan for this run holds no entry {name}")
    if entry.get("decision") != "run":
        raise Refusal(f"the plan decided {entry.get('decision')!r} for {name}, so nothing should have run it")
    return entry


def record(bucket, context, entries, engines):
    held = document(context, entries, engines)
    body = canonical.encode(held)
    name = location(context)
    bucket.create(name, body)
    bucket.put(standing(context), body)
    return {"plan": name, "standing": standing(context), "entries": len(held["entries"])}
