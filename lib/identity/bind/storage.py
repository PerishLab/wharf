import json

from lib.content import canonical
from lib.identity.bind import control, publication
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


def planner(context, reference):
    query = plan.provenance(context, reference)
    identity = {name: query[name] for name in ("run", "attempt", "wharf")}
    control.trusted(identity)
    job = publication.selected(publication.jobs(identity), "[plan] entries")
    require(job.get("status") == "completed" and job.get("conclusion") == "success", "referenced planner did not succeed")
    steps = job.get("steps")
    require(isinstance(steps, list), "referenced planner steps are missing")
    step = publication.selected(steps, "[record] plan")
    require(step.get("status") == "completed" and step.get("conclusion") == "success", "referenced plan was not recorded")
    return query


def origin(context):
    fields = {"repository", "marker", "wharf", "run", "attempt"}
    require(isinstance(context, dict) and set(context) in (fields, fields | {"plan"}), "binary workload producer context is incomplete")
    control.location(context["repository"], context["marker"])
    control.trusted({field: context[field] for field in ("run", "attempt", "wharf")})
    return planner(context, context["plan"]) if "plan" in context else context


def producer(bucket, loaded, name):
    context = loaded["record"].get("context")
    query = origin(context)
    document = plan.referenced(bucket, context, context["plan"]) if "plan" in context else plan.read(bucket, query)
    require(all(document["context"].get(field) == value for field, value in query.items()), "binary workload producer plan context differs")
    held = document["entries"].get(name, {})
    require(held.get("key") == loaded["key"] and held.get("decision") == "run", "binary workload differs from its producer's native plan")


def summary(loaded):
    return {"key": loaded["key"], "basis": canonical.digest(loaded["basis"]), "implementation": canonical.digest(loaded["implementation"]), "record": loaded["record_sha256"], "producer": loaded["record"]["context"]}


def published(proof, record):
    require(("plan" in proof) == ("plan" in record), "proof publication plan presence differs")
    if "plan" in record:
        require(proof["plan"] == record["plan"], "proof publication plan reference differs")
        control.trusted(control.observer(proof["publisher"]), "publisher")
        planner(dict(proof["source"], **proof["publisher"]), proof["plan"])
