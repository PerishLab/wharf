import json
import re

from lib.content import canonical, marker
from lib.refusal import Refusal
from lib.store import workload

SCHEMA = 1
PUBLISHED = ("npm", "oci", "chart", "cargo", "release", "channel", "cfworker")
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


def publication(context):
    return f"{named(context)}/publication.json"


def combination(verified):
    fields = {"schema", "source", "head", "tree", "packages", "controls", "domain", "guard"}
    if not isinstance(verified, dict) or set(verified) != fields or verified["schema"] != "wharf.release.snapshot/v1":
        raise Refusal("publication requires the verified release snapshot")
    return {field: verified[field] for field in sorted(fields - {"guard"})}


def matching(bucket, context, verified):
    key = publication(context)
    if bucket.exists(key) and bucket.get(key) != canonical.encode(combination(verified)):
        raise Refusal("this immutable marker began publication with another verified combination")


def reserve(bucket, document):
    context = document["context"]
    verified = context.get("snapshot")
    if combination(verified)["source"] != {field: context[field] for field in ("repository", "marker", "commit", "tree")}:
        raise Refusal("publication snapshot differs from the release identity")
    matching(bucket, context, verified)
    workload.settle(bucket, publication(context), canonical.encode(combination(verified)))


def legacy(bucket, context, entries, observed):
    if bucket.exists(publication(context)):
        return
    published = any(item.get("existing") == "true" or (item.get("decision") == "skip" and item.get("presence") == "present") for name, item in observed.items() if name != "channel")
    pending = any(entries.get(name, {}).get("decision") == "run" for name in PUBLISHED if name != "channel")
    if published and pending:
        raise Refusal("existing media has no verified publication combination; this marker requires explicit recovery review")


def depth(entries, name, held):
    if name not in held:
        held[name] = 1 + max((depth(entries, consumed, held) for consumed in entries[name].get("consumes", [])), default=0)
    return held[name]


def layered(entries):
    held = {}
    levels = [depth(entries, name, held) for name in entries if name not in PUBLISHED]
    layers = [sorted(name for name in entries if name not in PUBLISHED and held[name] == level) for level in range(1, max(levels, default=0) + 1)]
    return {"layers": layers, "last": sorted(name for name in entries if name in PUBLISHED)}


def document(context, entries, engines):
    return {"schema": SCHEMA, "context": context, "engines": engines, "entries": {name: entries[name] for name in sorted(entries)}, "shape": layered(entries)}


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
    return {"plan": name, "standing": standing(context), "entries": len(held["entries"]), "shape": held["shape"]}
