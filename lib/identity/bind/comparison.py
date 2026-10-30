import json
from pathlib import Path

from lib.content import canonical, marker
from lib.identity.bind import artifacts, control, inventory
from lib.identity.bind.spec import TARGET, entry, issuer, require
from lib.media import release
from lib.store import plan


def read(repository, version):
    _, authority, root = control.location(repository, version)
    index = json.loads(release.fetch(f"{authority}/{root}/build/linux-x64.json"))
    require(index.get("schema") == "wharf.binary.discovery/v1" and index.get("target") == TARGET and index.get("authority") == authority and index.get("source", {}).get("repository") == repository and index["source"].get("marker") == version, "Linux association discovery identity differs")
    control.observer(index.get("observer"))
    reference = index.get("proof", {})
    require(reference.get("url") == authority + "/v1/objects/sha256/" + str(reference.get("sha256")) + "/linux-association.json", "Linux association proof reference is foreign")
    raw = release.fetch(reference["url"])
    require(entry(raw) == {name: reference.get(name) for name in ("size", "sha256")}, "Linux association proof bytes differ")
    proof = json.loads(raw)
    require(isinstance(proof, dict) and set(proof) == {"schema", "issuer", "source", "publisher", "resolution", "target", "declaration", "seal", "archive", "files", "binaries", "build", "binding", "evidence"}, "Linux association proof fields differ")
    require(proof.get("schema") == "wharf.binary.association/v1" and proof.get("issuer") == issuer() and proof.get("target") == TARGET, "Linux association policy or implementation is unsupported")
    source = control.source(proof["source"])
    require(index["source"] == source, "Linux association discovery points to another source")
    require(source["repository"] == repository and source["marker"] == version, "Linux association proof belongs to another publication")
    record = json.loads(release.fetch(f"{authority}/{root}/distribution.json"))
    require(record.get("state") == "complete" and {name: record.get(name) for name in source} == source and record.get("completed") == proof["publisher"] and canonical.digest(plan.lineage(dict(record.get("resolution", {}).get("binaries", {}), guard={}))) == proof["resolution"], "Linux association publication context changed")
    declaration = inventory.validate(proof["declaration"])
    require(proof["seal"].get("url") == f"{authority}/{root}/seal.json", "Linux association seal reference is foreign")
    raw = release.fetch(proof["seal"]["url"])
    require(entry(raw) == {name: proof["seal"].get(name) for name in ("size", "sha256")}, "Linux association seal bytes differ")
    seal = json.loads(raw)
    require(seal.get("product") == repository.split("/")[1] and seal.get("releaseVersion") == version and seal.get("channel") == marker.channel(version) and seal.get("commit") == source["commit"], "Linux association seal identity differs")
    require(proof["archive"] == seal.get("artifacts", {}).get("linux-x64") and str(proof["archive"].get("url", "")).startswith(authority + "/v1/objects/sha256/"), "Linux association archive reference differs")
    raw = release.fetch(proof["archive"]["url"])
    require(entry(raw) == {name: proof["archive"].get(name) for name in ("size", "sha256")}, "Linux association archive bytes differ")
    files = artifacts.unpack(raw, declaration["installed"])
    require(proof["files"] == {name: entry(body) for name, body in files.items()} and set(proof["binaries"]) == set(declaration["built"]), "Linux association file coverage differs")
    restored = {}
    for name, body in files.items():
        original, facts = artifacts.unbound(body, source, proof["build"]["key"])
        require(facts == proof["binaries"].get(name), "Linux association binary bytes differ from its facts")
        restored[name] = original
    return proof, restored


def associated(left, right, before, after):
    require(all(left["source"][name] == right["source"][name] for name in ("repository", "commit", "tree")), "Linux association source changed")
    require(left["declaration"] == right["declaration"], "Linux association declaration or complete inventory changed")
    require(left["resolution"] == right["resolution"], "Linux association effective publication resolution changed")
    require(all(left["build"][name] == right["build"][name] for name in ("key", "basis", "implementation", "record")), "Linux association effective build basis or recorded content changed")
    require(before == after and set(before) == set(left["declaration"]["installed"]), "Linux association actual unbound bytes differ")
    fields = ("origin", "region", "size", "unbound", "outside")
    require(set(left["binaries"]) == set(right["binaries"]) == set(left["declaration"]["built"]), "Linux association complete build coverage changed")
    require(all(isinstance(held["binaries"][name], dict) for held in (left, right) for name in left["binaries"]), "Linux association build facts are invalid")
    require(all({field: left["binaries"][name].get(field) for field in fields} == {field: right["binaries"][name].get(field) for field in fields} for name in left["binaries"]), "Linux association non-installed build content changed")


def compare(repository, candidate, stable):
    require(marker.channel(candidate) in ("alpha", "beta", "rc") and marker.channel(stable) == "stable", "Linux association compares non-stable content to stable content only")
    left, before = read(repository, candidate)
    right, after = read(repository, stable)
    associated(left, right, before, after)
    return {"schema": "wharf.binary.comparison/v1", "associated": True, "target": TARGET, "repository": repository, "candidate": candidate, "stable": stable, "source": {name: right["source"][name] for name in ("commit", "tree")}, "build": right["build"], "installed": right["declaration"]["installed"], "archives": [left["archive"], right["archive"]], "proofs": [canonical.digest(left), canonical.digest(right)], "promotion": False}


def run(held):
    result = compare(held["repository"], held["reference"], held["marker"])
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "comparison.json").write_bytes(canonical.encode(result))
    return result
