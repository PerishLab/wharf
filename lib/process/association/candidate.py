import json
import subprocess
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical, implementation
from lib.identity.bind import candidate as legacy
from lib.identity.bind import storage, verification
from lib.identity.bind.spec import digest
from lib.media import archive, release
from lib.process.association import binary, chain, control, managers, public
from lib.process.association.spec import LIMIT, TARGETS, document, entry, filename, require, sha, target, Unavailable
from lib.refusal import Refusal
from lib.store import plan, r2, workload

SCHEMA = "wharf.binary.native.candidate/v2"
MATERIAL = "wharf.binary.native.candidate.material/v2"
FILES = {"result.json", "verification.json"}


def code():
    return implementation.resourced(["lib.process.association.candidate"], ["manager/association.json", "identity/format.json", "releases.json", "build/config.json", "manager/unix.sh.in", "manager/windows.ps1.in"])


def assess(bucket, selected, observer):
    basis = {"schema": legacy.MATERIAL, "observer": observer, "request": selected}
    linux, linux_reference = legacy.restore(bucket, workload.key(basis, legacy.code()), observer)
    require(linux.get("associated") is True, "native candidate needs exact independently verified Linux candidate evidence")
    planned, producer = verification.planned(bucket, selected)
    source = {name: planned["context"][name] for name in legacy.FIELDS}
    model = chain.frozen(planned, source)
    anticipated = managers.anticipated(source, model)
    resolution = canonical.digest(plan.lineage(dict(planned["context"]["snapshot"], guard={})))
    results, evidence = {}, {"linux": linux_reference, "plan": planned}
    for name in model["targets"]:
        if name not in TARGETS or not model["targets"][name]["installed"]:
            continue
        facts = chain.verify(bucket, source, planned, (model, name))
        reference, before = public.read(selected["repository"], selected["reference"], name)
        normalized = {file: binary.normalized(facts["binding"]["files"][filename(file, name)], file, name) for file in model["targets"][name]["installed"]}
        subject = {"source": source, "target": name, "declaration": model, "resolution": resolution, "build": storage.summary(facts["build"]), "binaries": facts["binaries"], "managers": anticipated}
        public.associated(reference, subject, before, normalized)
        members = [(file + (".exe" if target(name)["unit"] == "windows" else ""), facts["binding"]["files"][filename(file, name)]) for file in model["targets"][name]["installed"]]
        body = archive.pack(members, target(name)["format"])
        _, authority, _ = legacy.control.location(source["repository"], source["marker"])
        expected = release.remote(authority, "v1/objects/sha256/" + sha(body) + "/" + source["repository"].split("/")[1] + "-" + name + "." + target(name)["format"], body, release.MIMES[target(name)["format"]])
        results[name] = {"associated": True, "reference": canonical.digest(reference), "expected": expected, "binaries": facts["binaries"]}
        evidence[name] = {field: {key: facts[field][key] for key in ("key", "basis", "implementation", "record", "record_sha256", "receipt")} for field in ("build", "binding")}
    require(set(results) == {name for name in model["targets"] if name in TARGETS and model["targets"][name]["installed"]}, "native candidate target coverage differs")
    return {"schema": SCHEMA, "state": "associated", "associated": True, "source": source, "plan": producer, "linux": linux_reference, "targets": results, "managers": anticipated, "unassessed": ["oci", "chart", "cargo", "npm", "cfworker", "deb", "published-stable-readback"], "promotion": False}, evidence


def produced(bucket, basis):
    evidence = {}
    try:
        result, evidence = assess(bucket, basis["request"], basis["observer"])
    except Unavailable as failure:
        result = {"schema": SCHEMA, "state": "unknown", "associated": False, "reason": str(failure), "failure": {"kind": failure.kind, **failure.details}, "promotion": False}
    except Refusal as failure:
        result = {"schema": SCHEMA, "state": "refused", "associated": False, "reason": str(failure), "promotion": False}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result = {"schema": SCHEMA, "state": "unknown", "associated": False, "reason": "native candidate material is unavailable or malformed", "promotion": False}
    result.update(observer=basis["observer"], request=basis["request"])
    key = workload.key(basis, code())
    context = {name: basis[name] for name in ("observer", "request")}
    bodies = {"result.json": canonical.encode(result), "verification.json": canonical.encode(evidence)}
    expected = {"hash_version": 1, "key": key, "context": context, "files": {name: entry(body) for name, body in bodies.items()}}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, body in bodies.items():
            (root / name).write_bytes(body)
        workload.publish(bucket, key, workload.Produced(root, basis, context))
    prefix = workload.prefix(key)
    require(bucket.get(prefix + "basis.json") == canonical.encode(basis) and bucket.get(prefix + "record.json") == canonical.encode(expected) and all(bucket.get(prefix + "blobs/" + name) == body for name, body in bodies.items()), "native candidate material readback differs")
    return result, {"key": key, "record": canonical.digest(expected), "result": entry(bodies["result.json"])}


def restore(bucket, key, observer):
    require(digest(key), "native candidate key is invalid")
    prefix = workload.prefix(key)
    raw_basis, raw_record = bucket.get(prefix + "basis.json"), bucket.get(prefix + "record.json")
    require(len(raw_basis) <= 1048576 and len(raw_record) <= 1048576, "native candidate documents exceed bounds")
    basis, record = document(raw_basis), document(raw_record)
    require(raw_basis == canonical.encode(basis) and raw_record == canonical.encode(record), "native candidate documents are not canonical")
    require(set(basis) == {"schema", "observer", "request"} and basis["schema"] == MATERIAL and basis["observer"] == observer and workload.key(basis, code()) == key, "native candidate basis or attempt differs")
    legacy.request(basis["request"])
    context = {name: basis[name] for name in ("observer", "request")}
    require(set(record) == {"hash_version", "key", "context", "files"} and type(record["hash_version"]) is int and record["hash_version"] == 1 and record["key"] == key and record["context"] == context and set(record["files"]) == FILES, "native candidate record identity differs")
    require(all(isinstance(value, dict) and set(value) == {"size", "sha256"} and type(value["size"]) is int and 0 < value["size"] <= 4194304 and digest(value["sha256"]) for value in record["files"].values()), "native candidate file bounds differ")
    bodies = {name: bucket.get(prefix + "blobs/" + name) for name in FILES}
    require(all(entry(body) == record["files"][name] for name, body in bodies.items()), "native candidate bytes differ from record")
    result = document(bodies["result.json"])
    require(canonical.encode(result) == bodies["result.json"] and result.get("schema") == SCHEMA and result.get("observer") == observer and result.get("request") == basis["request"] and result.get("promotion") is False, "native candidate result identity differs")
    require(type(result.get("associated")) is bool and result.get("state") in ("associated", "refused", "unknown") and (result["state"] == "associated") == result["associated"], "native candidate result state differs")
    if result["associated"]:
        fresh, evidence = assess(bucket, basis["request"], observer)
        fresh.update(observer=observer, request=basis["request"])
        require(fresh == result and canonical.encode(evidence) == bodies["verification.json"], "native candidate differs from complete independent verification")
    return result, {"key": key, "record": sha(raw_record), "result": record["files"]["result.json"]}


def produce(held):
    observer, _ = control.active(held["observer"], "native-candidate")
    selected = legacy.request({name: held[name] for name in legacy.REQUEST})
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    result, reference = produced(r2.configured(), {"schema": MATERIAL, "observer": observer, "request": selected})
    answer = dict(result, evidence=reference)
    (root / "candidate.json").write_bytes(canonical.encode(answer))
    parameters.answer({"digest": reference["key"], "ready": str(result["associated"]).lower()})
    if not result["associated"]:
        raise Refusal(result["reason"])
    return answer


def consume(held):
    observer, _ = control.active(held["observer"], "native-candidate")
    result, reference = restore(r2.configured(), held["digest"], observer)
    answer = dict(result, evidence=reference)
    (Path(held["source"]).resolve() / "readback.json").write_bytes(canonical.encode(answer))
    return answer
