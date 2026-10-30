import json
import re
import subprocess
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical, implementation, marker
from lib.identity.bind import comparison, control, inventory, storage, verification
from lib.identity.bind.spec import FIELDS, LIMIT, TARGET, digest, entry, require, sha
from lib.refusal import Refusal
from lib.store import plan, r2, workload

ROLE = "build-candidate"
SCHEMA = "wharf.binary.candidate/v1"
MATERIAL = "wharf.binary.candidate.material/v1"
REQUEST = ("repository", "marker", "run", "attempt", "reference")
FILES = {"result.json", "verification.json", "candidate.tar.gz"}


def code():
    return implementation.resourced(["lib.identity.bind.candidate"], ["build.json", "identity/format.json", "releases.json", "parameters.json"])


def request(value):
    require(isinstance(value, dict) and set(value) == set(REQUEST) and all(isinstance(value[name], str) for name in REQUEST), "candidate request fields differ")
    control.location(value["repository"], value["marker"])
    require(marker.channel(value["marker"]) == "stable" and marker.channel(value["reference"]) in ("alpha", "beta", "rc"), "candidate verification needs a stable plan and non-stable reference")
    require(all(isinstance(value[name], str) and re.fullmatch(r"[1-9][0-9]*", value[name]) for name in ("run", "attempt")), "candidate plan run or attempt is invalid")
    return value


def assess(bucket, selected, evidence, retain=None):
    document, producer = verification.planned(bucket, selected)
    evidence["plan"] = document
    source = {name: document["context"][name] for name in FIELDS}
    declaration = inventory.planned(document["context"], source)
    facts = verification.chain(bucket, source, document, declaration)
    for name in ("build", "binding"):
        evidence[name] = {field: facts[name][field] for field in ("key", "basis", "implementation", "record", "record_sha256", "receipt")}
    body, expected = verification.packaged(source, facts)
    evidence["expected"] = expected
    if retain is not None:
        retain(body)
    reference, before, pinned = verification.published(selected["repository"], selected["reference"])
    evidence["reference"] = {"proof": reference, "reference": pinned}
    resolved = canonical.digest(plan.lineage(dict(document["context"]["snapshot"], guard={})))
    subject = {"source": source, "declaration": declaration, "resolution": resolved, "build": storage.summary(facts["build"]), "binding": storage.summary(facts["binding"]), "binaries": facts["binaries"]}
    after = {name: facts["build"]["files"][name + "-" + TARGET] for name in declaration["installed"]}
    comparison.associated(reference, subject, before, after)
    return dict(subject, state="associated", associated=True, plan=producer, reference=pinned, expected=expected), body


def material(bucket, basis, directory):
    context = {name: basis[name] for name in ("observer", "request")}
    key = workload.key(basis, code())
    files = {path.name: entry(path.read_bytes()) for path in directory.iterdir() if path.is_file()}
    expected = {"hash_version": 1, "key": key, "context": context, "files": files}
    workload.publish(bucket, key, workload.Produced(directory, basis, context))
    prefix = workload.prefix(key)
    require(bucket.get(prefix + "basis.json") == canonical.encode(basis) and bucket.get(prefix + "record.json") == canonical.encode(expected), "candidate material record differs from this attempt")
    require(all(bucket.get(prefix + "blobs/" + name) == (directory / name).read_bytes() for name in files), "candidate material bytes differ from this attempt")
    return {"key": key, "record": canonical.digest(expected), "result": files["result.json"]}


def produced(bucket, basis):
    evidence = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            result, _ = assess(bucket, basis["request"], evidence, lambda body: (root / "candidate.tar.gz").write_bytes(body))
        except Refusal as failure:
            result = {"state": "refused", "associated": False, "reason": str(failure)}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            result = {"state": "unknown", "associated": False, "reason": "candidate evidence is unavailable or malformed"}
        result.update(schema=SCHEMA, observer=basis["observer"], request=basis["request"], promotion=False)
        (root / "result.json").write_bytes(canonical.encode(result))
        if evidence:
            (root / "verification.json").write_bytes(canonical.encode(evidence))
        reference = material(bucket, basis, root)
    return result, reference


def restore(bucket, key, observer):
    prefix = workload.prefix(key)
    raw_basis, raw_record = bucket.get(prefix + "basis.json"), bucket.get(prefix + "record.json")
    require(len(raw_basis) <= 1048576 and len(raw_record) <= 1048576, "candidate material documents exceed verification bounds")
    basis, record = json.loads(raw_basis), json.loads(raw_record)
    require(raw_basis == canonical.encode(basis) and raw_record == canonical.encode(record), "candidate material documents are not canonical")
    require(set(basis) == {"schema", "observer", "request"} and basis["schema"] == MATERIAL and basis["observer"] == observer, "candidate material belongs to another attempt")
    request(basis["request"])
    require(workload.key(basis, code()) == key, "candidate material basis or implementation differs")
    context = {name: basis[name] for name in ("observer", "request")}
    require(set(record) == {"hash_version", "key", "context", "files"} and type(record["hash_version"]) is int and record["hash_version"] == 1 and record["key"] == key and record["context"] == context, "candidate material record identity differs")
    files = record["files"]
    require(isinstance(files, dict) and "result.json" in files and set(files) <= FILES, "candidate material file inventory is invalid")
    for name, value in files.items():
        limit = 2 * LIMIT if name == "candidate.tar.gz" else 4194304
        require(isinstance(value, dict) and set(value) == {"size", "sha256"} and type(value["size"]) is int and 0 < value["size"] <= limit and digest(value["sha256"]), "candidate material file identity is invalid")
    require(sum(value["size"] for value in files.values()) <= 4 * LIMIT, "candidate material exceeds verification bounds")
    bodies = {name: bucket.get(prefix + "blobs/" + name) for name in files}
    require(all(entry(body) == files[name] for name, body in bodies.items()), "candidate material bytes differ from their record")
    result = json.loads(bodies["result.json"])
    require(canonical.encode(result) == bodies["result.json"] and result.get("schema") == SCHEMA and result.get("observer") == observer and result.get("request") == basis["request"] and result.get("promotion") is False, "candidate result identity differs")
    require(type(result.get("associated")) is bool and result.get("state") in ("associated", "refused", "unknown") and (result["state"] == "associated") == result["associated"], "candidate result state is invalid")
    if result["associated"]:
        require(set(files) == FILES, "associated candidate material is incomplete")
        evidence = {}
        fresh, archive = assess(bucket, basis["request"], evidence)
        fresh.update(schema=SCHEMA, observer=observer, request=basis["request"], promotion=False)
        require(fresh == result and archive == bodies["candidate.tar.gz"] and canonical.encode(evidence) == bodies["verification.json"], "candidate material differs from complete independent verification")
    return result, {"key": key, "record": sha(raw_record), "result": files["result.json"]}


def produce(held):
    observer, _ = control.active(held["observer"], ROLE)
    selected = request({name: held[name] for name in REQUEST})
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    basis = {"schema": MATERIAL, "observer": observer, "request": selected}
    result, reference = produced(r2.configured(), basis)
    answer = dict(result, evidence=reference)
    (root / "candidate.json").write_bytes(canonical.encode(answer))
    parameters.answer({"digest": reference["key"], "ready": str(result["associated"]).lower()})
    if not result["associated"]:
        raise Refusal(result["reason"])
    return answer


def consume(held):
    observer, _ = control.active(held["observer"], ROLE)
    result, reference = restore(r2.configured(), held["digest"], observer)
    answer = dict(result, evidence=reference)
    (Path(held["source"]).resolve() / "readback.json").write_bytes(canonical.encode(answer))
    return answer
