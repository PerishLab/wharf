import json

from lib.content import canonical
from lib.identity.bind import control
from lib.identity.bind.spec import LIMIT, digest, entry, require, sha
from lib.store import plan, workload


def read(bucket, key, implementation):
    require(digest(key), "binary workload key is invalid")
    prefix = workload.prefix(key)
    raw_basis, raw_record = bucket.get(prefix + "basis.json"), bucket.get(prefix + "record.json")
    basis, record = json.loads(raw_basis), json.loads(raw_record)
    require(workload.key(basis, implementation) == key, "binary workload basis or implementation does not reproduce its key")
    require(raw_basis == canonical.encode(basis) and raw_record == canonical.encode(record), "binary workload documents are not canonical")
    require(type(record.get("hash_version")) is int and record["hash_version"] == 1 and record.get("key") == key, "binary workload record identity differs")
    files = record.get("files")
    require(isinstance(files, dict) and files and len(files) <= 65 and "receipt.json" in files, "binary workload file inventory is incomplete")
    for name, expected in files.items():
        require(isinstance(name, str) and name and "/" not in name and "\\" not in name and name not in (".", ".."), "binary workload path is invalid")
        require(isinstance(expected, dict) and set(expected) == {"size", "sha256"} and type(expected["size"]) is int and 0 < expected["size"] <= LIMIT and digest(expected["sha256"]), "binary workload file identity is invalid")
    require(sum(value["size"] for value in files.values()) <= 4 * LIMIT, "binary workload is too large to verify")
    bodies = {name: bucket.get(prefix + "blobs/" + name) for name in files}
    require(all(entry(body) == files[name] for name, body in bodies.items()), "binary workload bytes differ from their record")
    return {"key": key, "basis": basis, "implementation": implementation, "record": record, "record_sha256": sha(raw_record), "files": bodies, "receipt": json.loads(bodies["receipt.json"])}


def producer(bucket, loaded, name):
    context = loaded["record"].get("context")
    require(isinstance(context, dict) and set(context) == {"repository", "marker", "wharf", "run", "attempt"}, "binary workload producer context is incomplete")
    control.location(context["repository"], context["marker"])
    control.trusted({field: context[field] for field in ("run", "attempt", "wharf")})
    document = plan.read(bucket, context)
    require(all(document["context"].get(field) == value for field, value in context.items()), "binary workload producer plan context differs")
    held = document["entries"].get(name, {})
    require(held.get("key") == loaded["key"] and held.get("decision") == "run", "binary workload differs from its producer's native plan")


def summary(loaded):
    return {"key": loaded["key"], "basis": canonical.digest(loaded["basis"]), "implementation": canonical.digest(loaded["implementation"]), "record": loaded["record_sha256"], "producer": loaded["record"]["context"]}
