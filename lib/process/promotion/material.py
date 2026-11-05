import json

from lib.content import canonical, implementation
from lib.identity.bind.spec import entry, require, sha
from lib.process.promotion import assessment, reader
from lib.store import workload

SCHEMA = "wharf.promotion.material/v1"
RESULT = "wharf.promotion.preparation/v1"
FILES = {"result.json", "association.json", "verification.json", "candidate.tar.gz", "qualification.json", "credential.json"}


def code():
    return implementation.resourced(["lib.process.promotion.operation"], ["build/config.json", "identity/format.json", "releases.json", "parameters.json"])


def publish(bucket, basis, directory):
    context = {name: basis[name] for name in ("observer", "request", "started")}
    key = workload.key(basis, code())
    workload.publish(bucket, key, workload.Produced(directory, basis, context))
    return key


def restore(bucket, key, expected):
    prefix = workload.prefix(key)
    raw_basis, raw_record = bucket.get(prefix + "basis.json"), bucket.get(prefix + "record.json")
    require(len(raw_basis) <= 1048576 and len(raw_record) <= 1048576, "promotion material metadata exceeds bounds")
    basis, record = json.loads(raw_basis), json.loads(raw_record)
    require(raw_basis == canonical.encode(basis) and raw_record == canonical.encode(record), "promotion material is not canonical")
    require(basis == expected and workload.key(basis, code()) == key, "promotion material belongs to another source, attempt or implementation")
    context = {name: basis[name] for name in ("observer", "request", "started")}
    require(set(record) == {"hash_version", "key", "context", "files"} and type(record["hash_version"]) is int and record["hash_version"] == 1 and record["key"] == key and record["context"] == context, "promotion material record identity differs")
    files = record["files"]
    require(isinstance(files, dict) and "result.json" in files and set(files) <= FILES, "promotion material file inventory differs")
    for name, ref in files.items():
        limit = 536870912 if name == "candidate.tar.gz" else 4194304
        require(isinstance(ref, dict) and set(ref) == {"size", "sha256"} and type(ref["size"]) is int and 0 < ref["size"] <= limit, "promotion material size is invalid")
    require(sum(ref["size"] for ref in files.values()) <= 553648128, "promotion material total exceeds bounds")
    bodies = {name: bucket.get(prefix + "blobs/" + name) for name in files}
    require(all(entry(body) == files[name] for name, body in bodies.items()), "promotion material bytes differ")
    result = json.loads(bodies["result.json"])
    require(canonical.encode(result) == bodies["result.json"] and result["schema"] == RESULT and result["observer"] == basis["observer"] and result["request"] == basis["request"] and result["started"] == basis["started"] and result["promotion"] is False and result["complete_publication"] is False, "promotion result identity differs")
    require(result["state"] in ("prepared", "refused", "unknown"), "promotion result state is invalid")
    fields = {"schema", "observer", "request", "started", "promotion", "qualification", "association", "complete_publication", "state"}
    extra = {"identity", "plan", "scope", "unassessed"} if result["state"] == "prepared" else {"reason"}
    require(set(result) == fields | extra, "promotion result fields differ")
    require(type(result["qualification"]) is bool and type(result["association"]) is bool, "promotion result judgments are not boolean")
    if result["state"] == "prepared":
        require(set(bodies) == FILES and result["qualification"] is True and result["association"] is True and result["complete_publication"] is False, "promotion preparation is incomplete")
        fresh, verified = assessment.prepare(bucket, basis["request"], basis["observer"])
        require({name: result[name] for name in fresh} == fresh, "promotion material facts differ from independent verification")
        require(all(verified[name] == bodies[name] for name in FILES - {"result.json", "qualification.json"}), "promotion material differs from independent current verification")
        require(reader.pins(json.loads(verified["qualification.json"])) == reader.pins(json.loads(bodies["qualification.json"])), "promotion qualification facts moved during readback")
    else:
        require(set(bodies) == {"result.json"} and result["qualification"] is False and result["association"] is False, "failed promotion preparation claims successful coverage")
    return result, {"key": key, "record": sha(raw_record), "result": entry(bodies["result.json"])}
