import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lib.content import canonical
from lib.refusal import Conflict, Refusal

HASH_VERSION = 1


@dataclass(frozen=True)
class Produced:
    directory: Path
    basis: dict
    context: dict


def key(basis, implementation):
    return canonical.digest({"hash_version": HASH_VERSION, "basis": basis, "implementation": implementation})


def prefix(workload):
    if len(workload) != 64 or any(character not in "0123456789abcdef" for character in workload):
        raise Refusal(f"workload key {workload!r} is not a sha256 hex digest")
    return f"workload/{HASH_VERSION}/{workload}/"


def reusable(bucket, workload):
    return bucket.exists(prefix(workload) + "record.json")


def settle(bucket, name, body):
    try:
        bucket.create(name, body)
    except Conflict:
        if bucket.get(name) != body:
            raise Refusal(f"{name} already holds different bytes; the workload is not deterministic")


def publish(bucket, workload, produced):
    files = sorted(path for path in Path(produced.directory).iterdir() if path.is_file())
    if not files:
        raise Refusal(f"{produced.directory} holds no files to record")
    base = prefix(workload)
    settle(bucket, base + "basis.json", canonical.encode(produced.basis))
    entries = {}
    for path in files:
        body = path.read_bytes()
        settle(bucket, base + "blobs/" + path.name, body)
        entries[path.name] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    record = {"hash_version": HASH_VERSION, "key": workload, "files": entries, "context": produced.context}
    try:
        bucket.create(base + "record.json", canonical.encode(record))
    except Conflict:
        return {"key": workload, "state": "already-recorded"}
    return {"key": workload, "state": "recorded", "files": entries}


def fetch(bucket, workload, destination):
    base = prefix(workload)
    if not bucket.exists(base + "record.json"):
        raise Refusal(f"workload {workload} has no record")
    record = json.loads(bucket.get(base + "record.json"))
    destination = Path(destination)
    if destination.exists():
        raise Refusal(f"{destination} already exists")
    destination.mkdir(parents=True)
    for name, expected in sorted(record["files"].items()):
        body = bucket.get(base + "blobs/" + name)
        if hashlib.sha256(body).hexdigest() != expected["sha256"] or len(body) != expected["size"]:
            raise Refusal(f"{name} under {workload} does not match its record")
        (destination / name).write_bytes(body)
    return {"key": workload, "files": record["files"]}
