import json
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.identity.bind import artifacts, control, inventory, storage, verification
from lib.identity.bind.spec import POLICY, TARGET, code, entry, issuer, require, sha
from lib.media import release
from lib.refusal import Conflict, Refusal
from lib.store import plan, r2, workload


def planned(bucket, public):
    context = dict(public["source"], **public["publisher"])
    document = plan.read(bucket, context)
    require(all(document["context"].get(name) == value for name, value in context.items()), "publication plan belongs to another source or attempt")
    expected = public["distribution"].get("resolution", {}).get("binaries")
    require(expected is not None and plan.combination(document["context"]["snapshot"]) == expected, "publication plan differs from the complete binary resolution")
    return document


def verified(bucket, public, document):
    declaration = inventory.resolve(public, document)
    facts = verification.chain(bucket, public["source"], document, declaration)
    reference = public["seal"].get("artifacts", {}).get("linux-x64")
    _, authority, _ = control.location(public["source"]["repository"], public["source"]["marker"])
    require(isinstance(reference, dict) and str(reference.get("url", "")).startswith(authority + "/v1/objects/sha256/"), "public Linux archive reference differs")
    raw = release.fetch(reference["url"])
    require(entry(raw) == {name: reference.get(name) for name in ("size", "sha256")}, "public Linux archive bytes differ from the seal")
    files = artifacts.packaged(raw, declaration["installed"], facts["binding"])
    return dict(facts, archive=reference, files=files)


def material(bucket, public, facts):
    basis = {"schema": "wharf.binary.material/v1", "policy": POLICY, "source": public["source"], "seal": public["seal_ref"]["sha256"], "build": facts["build"]["key"], "binding": facts["binding"]["key"]}
    key = workload.key(basis, code())
    evidence = {"declaration": facts["declaration"], "binaries": facts["binaries"], "archive": facts["archive"], "files": facts["files"]}
    for name in ("build", "binding"):
        loaded = facts[name]
        evidence[name] = {field: loaded[field] for field in ("key", "basis", "implementation", "record", "record_sha256", "receipt")}
    context = {"source": public["source"], "publisher": public["publisher"], "policy": POLICY}
    body = canonical.encode(evidence)
    files = {"verification.json": entry(body)}
    expected = {"hash_version": 1, "key": key, "context": context, "files": files}
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "verification.json").write_bytes(body)
        workload.publish(bucket, key, workload.Produced(Path(directory), basis, context))
    prefix = workload.prefix(key)
    require(bucket.get(prefix + "basis.json") == canonical.encode(basis) and bucket.get(prefix + "record.json") == canonical.encode(expected) and bucket.get(prefix + "blobs/verification.json") == body, "existing binary material differs from this complete verification")
    return {"key": key, "record": canonical.digest(expected), "verification": entry(body)}


def statement(public, facts, recorded):
    resolution = plan.lineage(dict(public["distribution"]["resolution"]["binaries"], guard={}))
    return {"schema": "wharf.binary.association/v1", "issuer": issuer(), "source": public["source"], "publisher": public["publisher"], "resolution": canonical.digest(resolution), "target": TARGET, "declaration": facts["declaration"], "seal": public["seal_ref"], "archive": facts["archive"], "files": facts["files"], "binaries": facts["binaries"], "build": storage.summary(facts["build"]), "binding": storage.summary(facts["binding"]), "evidence": recorded}


def subject(value):
    return {name: held for name, held in value.items() if name not in ("issuer", "evidence")}


def advance(bucket, key, proposed):
    index, proof = proposed
    for _ in range(4):
        body, etag = bucket.snapshot(key)
        if body is not None:
            held = json.loads(body)
            require(held.get("schema") == "wharf.binary.discovery/v1" and held.get("source") == index["source"] and held.get("target") == TARGET, "existing Linux discovery identity differs")
            control.observer(held.get("observer"))
            control.started(held.get("started"))
            reference = held.get("proof", {})
            require(reference.get("url") == index["authority"] + "/v1/objects/sha256/" + str(reference.get("sha256")) + "/linux-association.json", "existing Linux proof reference is foreign")
            previous = release.fetch(reference["url"])
            require(entry(previous) == {name: reference.get(name) for name in ("size", "sha256")} and subject(json.loads(previous)) == subject(proof), "existing Linux proof records different build or publication content")
            before = (held["started"], int(held["observer"]["run"]), int(held["observer"]["attempt"]))
            after = (index["started"], int(index["observer"]["run"]), int(index["observer"]["attempt"]))
            if before > after:
                return "kept"
            if before == after:
                require(held == index, "one native attempt has conflicting Linux discovery")
                return "present"
        try:
            bucket.swap(key, canonical.encode(index), etag)
            return "advanced"
        except Conflict:
            continue
    raise Refusal("Linux discovery changed repeatedly")


def publish(public, value, observer, started):
    source = public["source"]
    name, authority, root = control.location(source["repository"], source["marker"])
    body = canonical.encode(value)
    key = "v1/objects/sha256/" + sha(body) + "/linux-association.json"
    bucket = r2.writer(name, "RELEASES")
    try:
        bucket.create(key, body, {"Content-Type": release.JSON})
    except Conflict:
        require(bucket.get(key) == body, "immutable Linux association proof already holds different bytes")
    require(release.fetch(f"{authority}/{key}") == body, "public Linux association proof readback differs")
    reference = release.remote(authority, key, body, release.JSON)
    index = {"schema": "wharf.binary.discovery/v1", "source": source, "target": TARGET, "authority": authority, "observer": observer, "started": started, "proof": reference}
    advance(bucket, root + "/build/linux-x64.json", (index, value))
    held = json.loads(release.fetch(f"{authority}/{root}/build/linux-x64.json"))
    require(held.get("source") == source and held.get("proof", {}).get("sha256") == reference["sha256"], "public Linux discovery readback differs")
    return reference


def produce(held):
    observer, started = control.active(held["observer"])
    public = control.public(held)
    bucket = r2.configured()
    document = planned(bucket, public)
    facts = verified(bucket, public, document)
    recorded = material(bucket, public, facts)
    current = control.public(held)
    require(current == public, "publication changed during Linux association verification")
    result = publish(public, statement(public, facts, recorded), observer, started)
    parameters.answer({"ready": "true"})
    return {"state": "proven", "observer": observer, "source": public["source"], "proof": result, "evidence": recorded}
