import re

from lib import canonical
from lib.refusal import Refusal

MARKER = re.compile(r"v\d+\.\d+\.\d+(-(alpha|beta|rc)\.[1-9]\d*)?")
NAMED = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def named(context):
    if not NAMED.fullmatch(context["repository"]):
        raise Refusal(f"repository {context['repository']!r} is not owner/name")
    if not MARKER.fullmatch(context["marker"]):
        raise Refusal(f"marker {context['marker']!r} is malformed")
    return f"plan/{context['repository']}/{context['marker']}"


def location(context):
    return f"{named(context)}/{context['run']}-{context['attempt']}.json"


def standing(context):
    return f"{named(context)}/latest.json"


def document(context, entries, engines):
    return {"schema": 1, "context": context, "engines": engines, "entries": {name: entries[name] for name in sorted(entries)}}


def agreed(entries, observed):
    drift = []
    for name, entry in sorted(entries.items()):
        held = observed.get(name, {})
        for field in sorted(entry):
            if held.get(field) != entry[field]:
                drift.append(f"{name}.{field}: planned {entry[field]!r}, step reported {held.get(field)!r}")
    return drift


def record(bucket, context, entries, engines):
    held = document(context, entries, engines)
    body = canonical.encode(held)
    name = location(context)
    bucket.create(name, body)
    bucket.put(standing(context), body)
    return {"plan": name, "standing": standing(context), "entries": len(held["entries"])}
