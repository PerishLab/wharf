import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.identity.bind import control as identity
from lib.identity.bind import producer as legacy
from lib.identity.bind import storage
from lib.media import release
from lib.process.association import archive, chain, control, managers, public
from lib.process.association.spec import DISCOVERY, SCHEMA, TARGETS, code, document, entry, filename, issuer, require, sha, target, Unavailable
from lib.refusal import Conflict, Refusal
from lib.store import plan, r2, workload


def archive_facts(publication, facts):
    selected = facts["target"]
    _, authority, _ = identity.location(publication["source"]["repository"], publication["source"]["marker"])
    reference = publication["seal"].get("artifacts", {}).get(target(selected)["key"])
    raw = public.reference(reference, authority, "archive")
    files = archive.unpack(raw, facts["declaration"]["targets"][selected]["installed"], selected)
    require(all(body == facts["binding"]["files"][filename(name.removesuffix(".exe") if target(selected)["unit"] == "windows" else name, selected)] for name, body in files.items()), "native public archive differs from verified bound workload")
    return dict(facts, archive=reference, files={name: entry(body) for name, body in files.items()})


def material(bucket, publication, facts):
    basis = {"schema": "wharf.binary.native.material/v2", "source": publication["source"], "target": facts["target"], "archive": facts["archive"]["sha256"], "build": facts["build"]["key"], "binding": facts["binding"]["key"], "declaration": canonical.digest(facts["declaration"]), "managers": canonical.digest(facts["managers"])}
    key = workload.key(basis, code())
    evidence = {name: facts[name] for name in ("declaration", "binaries", "archive", "files", "managers")}
    for name in ("build", "binding"):
        evidence[name] = {field: facts[name][field] for field in ("key", "basis", "implementation", "record", "record_sha256", "receipt")}
    context = {"source": publication["source"], "publisher": publication["publisher"], "target": facts["target"], "issuer": issuer()}
    body = canonical.encode(evidence)
    expected = {"hash_version": 1, "key": key, "context": context, "files": {"verification.json": entry(body)}}
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "verification.json").write_bytes(body)
        workload.publish(bucket, key, workload.Produced(Path(directory), basis, context))
    prefix = workload.prefix(key)
    require(bucket.get(prefix + "basis.json") == canonical.encode(basis) and bucket.get(prefix + "record.json") == canonical.encode(expected) and bucket.get(prefix + "blobs/verification.json") == body, "native verification material differs from recorded bytes")
    return {"key": key, "record": canonical.digest(expected), "verification": entry(body)}


def statement(publication, facts, recorded):
    resolution = plan.lineage(dict(publication["distribution"]["resolution"]["binaries"], guard={}))
    result = {"schema": SCHEMA, "issuer": issuer(), "source": publication["source"], "publisher": publication["publisher"], "resolution": canonical.digest(resolution), "seal": publication["seal_ref"], "evidence": recorded}
    result.update({name: facts[name] for name in ("target", "declaration", "archive", "files", "binaries", "managers")})
    result.update({name: storage.summary(facts[name]) for name in ("build", "binding")})
    return result


def subject(value):
    return {name: held for name, held in value.items() if name not in ("issuer", "evidence")}


def advance(bucket, location, proposed):
    index, proof = proposed
    for _ in range(4):
        raw, etag = bucket.snapshot(location)
        if raw is not None:
            held = document(raw)
            require(set(held) == set(index) and held.get("schema") == DISCOVERY and held.get("source") == index["source"] and held.get("target") == index["target"] and held.get("authority") == index["authority"], "existing native discovery identity differs")
            identity.observer(held["observer"])
            identity.started(held["started"])
            before = public.reference(held["proof"], index["authority"], "proof")
            require(held["proof"]["name"] == "native-association.json" and subject(document(before)) == subject(proof), "existing native proof records different publication content")
            previous = (held["started"], int(held["observer"]["run"]), int(held["observer"]["attempt"]))
            current = (index["started"], int(index["observer"]["run"]), int(index["observer"]["attempt"]))
            if previous > current:
                return "kept"
            if previous == current:
                require(held == index, "one native attempt has conflicting discovery")
                return "present"
        try:
            bucket.swap(location, canonical.encode(index), etag)
            return "advanced"
        except Conflict:
            continue
    raise Refusal("native discovery changed repeatedly")


def publish(publication, proof, observer, started):
    source = publication["source"]
    bucket_name, authority, root = identity.location(source["repository"], source["marker"])
    body = canonical.encode(proof)
    location = "v1/objects/sha256/" + sha(body) + "/native-association.json"
    bucket = r2.writer(bucket_name, "RELEASES")
    try:
        bucket.create(location, body, {"Content-Type": release.JSON})
    except Conflict:
        require(bucket.get(location) == body, "immutable native proof already holds different bytes")
    reference = release.remote(authority, location, body, release.JSON)
    require(public.reference(reference, authority, "proof") == body, "public native proof readback differs")
    index = {"schema": DISCOVERY, "source": source, "target": proof["target"], "authority": authority, "observer": observer, "started": started, "proof": reference}
    location = root + "/build/native-" + target(proof["target"])["key"] + ".json"
    advance(bucket, location, (index, proof))
    require(document(public.fetched(authority + "/" + location, "discovery")) == index, "public native discovery readback differs")
    return reference


def produce(held):
    observer, started = control.active(held["observer"], "native-proof")
    publication = public.snapshot(held)
    bucket = r2.configured()
    document = legacy.planned(bucket, publication)
    model = chain.frozen(document, publication["source"])
    selected = [name for name in model["targets"] if name in TARGETS and model["targets"][name]["installed"]]
    if not selected:
        parameters.answer({"ready": "false"})
        return {"state": "not-declared", "source": publication["source"], "proofs": {}, "promotion": False}
    management = public.managed(publication, model)
    results = {}
    for name in selected:
        try:
            facts = chain.verify(bucket, publication["source"], document, (model, name))
            facts = archive_facts(publication, dict(facts, managers=management))
            recorded = material(bucket, publication, facts)
            require(public.snapshot(held) == publication and public.managed(publication, model) == management, "native publication changed during verification")
            control.active(held["observer"], "native-proof")
            results[name] = publish(publication, statement(publication, facts, recorded), observer, started)
        except Unavailable as failure:
            raise failure.during(selected=name, version=held["marker"]) from None
    parameters.answer({"ready": str(bool(results)).lower()})
    return {"state": "proven" if results else "not-declared", "source": publication["source"], "proofs": results, "promotion": False}
