"""Workload records in the object store, keyed by the plan's hash key contract.

Layout under workload/<hash_version>/<key>/:
  inputs.json   the effective inputs that produced the key
  blobs/<file>  the produced files
  record.json   written last; its presence is what makes a workload reusable
"""

import hashlib
import json
from pathlib import Path

from wharf.refusal import Refusal
from wharf.store.r2 import Conflict

HASH_VERSION = 1


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def key(inputs, implementation):
    return hashlib.sha256(canonical({"hash_version": HASH_VERSION, "inputs": inputs, "implementation": implementation})).hexdigest()


def implementation(*modules):
    return {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in modules}


def prefix(workload):
    if len(workload) != 64 or any(character not in "0123456789abcdef" for character in workload):
        raise Refusal(f"workload key {workload!r} is not a sha256 hex digest")
    return f"workload/{HASH_VERSION}/{workload}/"


def reusable(bucket, workload):
    return bucket.exists(prefix(workload) + "record.json")


def _settle(bucket, name, body):
    try:
        bucket.create(name, body)
    except Conflict:
        if bucket.get(name) != body:
            raise Refusal(f"{name} already holds different bytes; the workload is not deterministic")


def publish(bucket, workload, directory, inputs, context):
    directory = Path(directory)
    files = sorted(path for path in directory.iterdir() if path.is_file())
    if not files:
        raise Refusal(f"{directory} holds no files to record")
    base = prefix(workload)
    _settle(bucket, base + "inputs.json", canonical(inputs))
    entries = {}
    for path in files:
        body = path.read_bytes()
        _settle(bucket, base + "blobs/" + path.name, body)
        entries[path.name] = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    record = {"hash_version": HASH_VERSION, "key": workload, "files": entries, "context": context}
    try:
        bucket.create(base + "record.json", canonical(record))
    except Conflict:
        return {"key": workload, "state": "already-recorded"}
    return {"key": workload, "state": "recorded", "files": entries}
