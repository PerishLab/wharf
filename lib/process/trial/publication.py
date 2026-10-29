import hashlib
import json
import re

from lib.content import canonical, marker
from lib.media import release
from lib.process.trial import context
from lib.process.trial.admission import checked
from lib.refusal import Conflict, Refusal


def location(repository, version):
    checked(isinstance(repository, str) and re.fullmatch(r"PerishLab/[a-z0-9_-]+", repository), "qualification repository differs")
    checked(marker.channel(version) in ("alpha", "beta", "rc"), "qualification requires a non-stable release")
    placed = release.Release(repository, version, "", "")
    _, bucket, authority = release.place(placed)
    return bucket, authority, f"v1/releases/{marker.channel(version)}/{version}/windtun"


def path(repository, version, observer):
    observer = context.decode(observer)
    return location(repository, version)[2] + f"/attempts/{observer['run']}-{observer['attempt']}"


def reference(authority, key, body):
    return release.remote(authority, key, body, release.JSON)


def verified(entry, authority):
    checked(isinstance(entry, dict) and context.digest(entry.get("sha256")) and type(entry.get("size")) is int and entry["size"] > 0, "public qualification reference is invalid")
    checked(isinstance(entry.get("url"), str) and entry["url"].startswith(authority + "/") and not any(part in entry["url"] for part in ("/../", "?", "#")), "public qualification reference has a foreign authority")
    body = release.fetch(entry["url"])
    checked(len(body) == entry["size"] and hashlib.sha256(body).hexdigest() == entry["sha256"], "public qualification bytes differ")
    return json.loads(body)


def settle(bucket, key, value):
    body = canonical.encode(value)
    try:
        bucket.create(key, body, {"Content-Type": release.JSON})
    except Conflict:
        checked(bucket.get(key) == body, "immutable qualification address already holds different bytes")
    return body


def advance(bucket, key, value):
    for _ in range(4):
        body, etag = bucket.snapshot(key)
        if body is not None:
            held = json.loads(body)
            checked(held.get("schema") == "wharf.windtun.index/v1", "qualification index schema differs")
            context.decode(held.get("observer"))
            previous = [context.clock(held.get("started")), *[int(held["observer"][name]) for name in ("run", "attempt")]]
            proposed = [context.clock(value.get("started")), *[int(value["observer"][name]) for name in ("run", "attempt")]]
            if previous > proposed:
                return "kept"
            if previous == proposed:
                checked(held == value, "one attempt has conflicting qualification discovery")
                return "present"
        try:
            bucket.swap(key, canonical.encode(value), etag)
            return "advanced"
        except Conflict:
            continue
    raise Refusal("qualification discovery changed repeatedly")


def begin(bucket, authority, prepared, material):
    source = prepared["source"]
    root = path(source["repository"], source["marker"], prepared["observer"])
    intent = {"schema": "wharf.windtun.intent/v1", "observer": prepared["observer"], "started": prepared["started"], "source": source, "policy": context.POLICY, "scope": prepared["requirements"]["scope"], "preparation": material}
    body = settle(bucket, root + "/intent.json", intent)
    entry = reference(authority, root + "/intent.json", body)
    if intent["scope"] == "declared":
        index = {"schema": "wharf.windtun.index/v1", "observer": prepared["observer"], "started": prepared["started"], "intent": entry}
        advance(bucket, location(source["repository"], source["marker"])[2] + "/latest.json", index)
    checked(release.fetch(entry["url"]) == body, "public attempt intent readback differs")
    return intent


def read(repository, version, observer):
    _, authority, _ = location(repository, version)
    value = json.loads(release.fetch(f"{authority}/{path(repository, version, observer)}/intent.json"))
    checked(value.get("schema") == "wharf.windtun.intent/v1" and value.get("observer") == context.decode(observer) and value.get("policy") == context.POLICY, "qualification intent belongs to another attempt or policy")
    checked(value.get("source", {}).get("repository") == repository and value["source"].get("marker") == version, "qualification intent belongs to another release")
    return value


def finish(bucket, authority, intent, assessment):
    result, passed = assessment
    source = intent["source"]
    root = path(source["repository"], source["marker"], intent["observer"])
    value = dict(result, schema="wharf.windtun.result/v1", intent=canonical.digest(intent), observer=intent["observer"], source=source, credential=None)
    if passed is not None:
        body = settle(bucket, root + "/pass.json", passed)
        value["credential"] = reference(authority, root + "/pass.json", body)
    body = settle(bucket, root + "/result.json", value)
    entry = reference(authority, root + "/result.json", body)
    checked(release.fetch(entry["url"]) == body, "public qualification result readback differs")
    return dict(value, reference=entry)
