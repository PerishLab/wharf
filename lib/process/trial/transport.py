import hashlib
import json
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.process.trial import context, preparation, publication
from lib.process.trial.admission import checked
from lib.store import r2, workload

FILES = {"prepared.json", "admission.json", "runner.json", "execution.json", "report.json", "observation.json", "run.log", "install.log"}
LIMIT = 32 * 1024 * 1024


def basis(prepared, kind):
    return {"schema": "wharf.windtun.workload/v1", "kind": kind, "prepared": prepared}


def recorded(bucket, prepared, kind):
    key = context.address(prepared, kind)
    prefix = workload.prefix(key)
    checked(bucket.get(prefix + "basis.json") == canonical.encode(basis(prepared, kind)), "workload basis differs from prepared world")
    raw = bucket.get(prefix + "record.json")
    record = json.loads(raw)
    expected = {"observer": prepared["observer"], "kind": kind, "prepared": canonical.digest(prepared)}
    checked(record.get("hash_version") == 1 and record.get("key") == key and record.get("context") == expected, "workload record belongs to another attempt or world")
    files = record.get("files")
    allowed = {"prepared.json", "admission.json"} if kind == "preparation" else FILES
    checked(isinstance(files, dict) and {"prepared.json", "admission.json"} <= set(files) <= allowed, "workload has missing or unexpected evidence paths")
    for entry in files.values():
        checked(isinstance(entry, dict) and set(entry) == {"size", "sha256"} and type(entry["size"]) is int and 0 <= entry["size"] <= LIMIT and context.digest(entry["sha256"]), "workload file identity is invalid")
    checked(sum(entry["size"] for entry in files.values()) <= LIMIT, "workload evidence exceeds bounded size")
    return record, hashlib.sha256(raw).hexdigest()


def capture(evidence, destination):
    checked(evidence.is_dir() and not evidence.is_symlink(), "evidence directory must be regular")
    size = 0
    for name in sorted(FILES):
        source = evidence / name
        checked(not source.is_symlink(), "evidence symlinks refuse")
        if not source.exists():
            continue
        checked(source.is_file() and source.stat().st_size <= LIMIT, "evidence must be bounded regular files")
        body = source.read_bytes()
        size += len(body)
        checked(size <= LIMIT, "evidence exceeds bounded size")
        (destination / name).write_bytes(body)


def publish(bucket, prepared, kind, directory):
    preparation.validate_prepared(prepared)
    key = context.address(prepared, kind)
    held = {"observer": prepared["observer"], "kind": kind, "prepared": canonical.digest(prepared)}
    workload.publish(bucket, key, workload.Produced(directory, basis(prepared, kind), held))
    record, digest = recorded(bucket, prepared, kind)
    expected = {path.name: {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in Path(directory).iterdir()}
    checked(record["files"] == expected, "existing workload record differs from this capture")
    return {"key": key, "record": digest, "basis": canonical.digest(basis(prepared, kind))}


def retrieve(bucket, prepared, kind, destination):
    record, digest = recorded(bucket, prepared, kind)
    workload.fetch(bucket, context.address(prepared, kind), destination)
    root = Path(destination)
    checked(json.loads((root / "prepared.json").read_bytes()) == prepared and json.loads((root / "admission.json").read_bytes()) == prepared["admission"], "captured preparation or admission differs")
    return record, digest


def resolve(bucket, intent, destination):
    material = intent.get("preparation")
    checked(isinstance(material, dict) and set(material) == {"key", "record", "basis"} and all(context.digest(value) for value in material.values()), "intent preparation reference is invalid")
    raw = bucket.get(workload.prefix(material["key"]) + "basis.json")
    checked(hashlib.sha256(raw).hexdigest() == material["basis"], "intent preparation basis differs")
    held = json.loads(raw)
    checked(isinstance(held, dict) and set(held) == {"schema", "kind", "prepared"}, "intent preparation basis has unknown fields")
    prepared = preparation.validate_prepared(held["prepared"])
    checked(held == basis(prepared, "preparation") and context.address(prepared, "preparation") == material["key"], "intent preparation key differs")
    checked(prepared["observer"] == intent["observer"] and prepared["source"] == intent["source"] and prepared["started"] == intent["started"] and prepared["requirements"]["scope"] == intent["scope"], "intent preparation identity differs")
    _, digest = retrieve(bucket, prepared, "preparation", destination)
    checked(digest == material["record"], "intent preparation record differs")
    return prepared


def prepare(held):
    observer, jobs = context.observe(held["observer"], "windtun-prepare")
    evidence = Path(held["source"]).resolve() / "evidence"
    admitted = json.loads((evidence / "admission.json").read_bytes())
    prepared = preparation.freeze(admitted, observer, held["trialcase"], jobs[context.NAMES["windtun-prepare"]]["started_at"])
    (evidence / "prepared.json").write_bytes(canonical.encode(prepared))
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory)
        for name in ("prepared.json", "admission.json"):
            (destination / name).write_bytes((evidence / name).read_bytes())
        material = publish(r2.configured(), prepared, "preparation", destination)
    source = prepared["source"]
    bucket, authority, _ = publication.location(source["repository"], source["marker"])
    intent = publication.begin(r2.writer(bucket, "RELEASES"), authority, prepared, material)
    parameters.answer({"ready": "true"})
    return intent


def load(held):
    observer, jobs = context.observe(held["observer"], "windtun")
    context.succeeded(jobs.get(context.NAMES["windtun-prepare"], {}), ["[windtun] admit publisher", "[windtun] prepare attempt"])
    intent = publication.read(held["repository"], held["marker"], observer)
    evidence = Path(held["source"]).resolve() / "evidence"
    prepared = resolve(r2.configured(), intent, evidence)
    checked(prepared["started"] == jobs[context.NAMES["windtun-prepare"]].get("started_at"), "prepared start differs from native producer")
    preparation.unchanged(prepared)
    parameters.answer({"ready": "true"})
    return {"state": "loaded", "observer": observer, "prepared": canonical.digest(prepared)}


def store(held):
    observer, _ = context.observe(held["observer"], "windtun")
    intent = publication.read(held["repository"], held["marker"], observer)
    bucket = r2.configured()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        prepared = resolve(bucket, intent, root / "prepared")
        capture_root = root / "capture"
        capture_root.mkdir()
        capture(Path(held["source"]).resolve() / "evidence", capture_root)
        checked(json.loads((capture_root / "prepared.json").read_bytes()) == prepared and json.loads((capture_root / "admission.json").read_bytes()) == prepared["admission"], "execution changed its prepared identity")
        material = publish(bucket, prepared, "execution", capture_root)
        record, _ = recorded(bucket, prepared, "execution")
    digest = context.bundle(record["files"], observer)
    parameters.answer({"digest": digest})
    return dict(material, digest=digest)
