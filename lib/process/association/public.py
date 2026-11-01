from pathlib import Path

from lib.content import canonical, marker
from lib.identity.bind import control
from lib.identity.bind.spec import digest
from lib.media import release
from lib.process.association import archive, binary, chain, managers
from lib.process.association.spec import DISCOVERY, LIMIT, SCHEMA, TARGETS, document, entry, fetch, issuer, require, sha, target
from lib.store import plan

FIELDS = {"schema", "issuer", "source", "publisher", "resolution", "target", "declaration", "seal", "archive", "files", "binaries", "build", "binding", "evidence", "managers"}


def snapshot(held):
    _, authority, root = control.location(held["repository"], held["marker"])
    record = document(fetch(f"{authority}/{root}/distribution.json"))
    require(type(record.get("schema")) is int and record["schema"] == 1, "native distribution schema differs")
    source = control.source({name: record.get(name) for name in control.FIELDS})
    require(source["repository"] == held["repository"] and source["marker"] == held["marker"] and record.get("state") == "complete" and record.get("media", {}).get("binaries") in ("published", "present"), "native association needs the exact complete binary distribution")
    publisher = control.observer(record.get("completed"))
    require(held["run"] == "null" or held["run"] == publisher["run"], "native publication run differs from request")
    control.trusted(publisher)
    raw = fetch(f"{authority}/{root}/seal.json")
    seal = document(raw)
    require(type(seal.get("schema")) is int and seal["schema"] == 1 and all(seal.get(name) == value for name, value in (("product", source["repository"].split("/")[1]), ("releaseVersion", source["marker"]), ("channel", marker.channel(source["marker"])), ("commit", source["commit"]))), "native public seal identity differs")
    return {"source": source, "publisher": publisher, "distribution": record, "seal": seal, "seal_ref": release.remote(authority, root + "/seal.json", raw, release.JSON)}


def reference(value, authority):
    require(isinstance(value, dict) and set(value) == {"url", "name", "mime", "size", "sha256"} and type(value["size"]) is int and 0 < value["size"] <= 2 * LIMIT and digest(value["sha256"]), "native object reference fields or bounds differ")
    require(isinstance(value["name"], str) and value["name"] not in ("", ".", "..") and "/" not in value["name"] and "\\" not in value["name"] and value["url"] == f"{authority}/v1/objects/sha256/{value['sha256']}/{value['name']}", "native object reference is foreign")
    raw = fetch(value["url"], value["size"])
    require(entry(raw) == {name: value[name] for name in ("size", "sha256")}, "native object bytes differ from reference")
    return raw


def read(repository, version, selected):
    specification = target(selected)
    _, authority, root = control.location(repository, version)
    url = f"{authority}/{root}/build/native-{specification['key']}.json"
    index_raw = fetch(url)
    index = document(index_raw)
    require(isinstance(index, dict) and set(index) == {"schema", "source", "target", "authority", "observer", "started", "proof"} and index["schema"] == DISCOVERY and index["target"] == selected and index["authority"] == authority, "native discovery fields or target differ")
    control.source(index["source"])
    require(index["source"]["repository"] == repository and index["source"]["marker"] == version, "native discovery belongs to another publication")
    control.trusted(control.observer(index["observer"]))
    control.started(index["started"])
    require(isinstance(index["proof"], dict) and type(index["proof"].get("size")) is int and index["proof"]["size"] <= 4194304, "native proof exceeds document bounds")
    raw = reference(index["proof"], authority)
    require(len(raw) <= 4194304 and index["proof"]["name"] == "native-association.json", "native proof is outside policy bounds")
    proof = document(raw)
    require(isinstance(proof, dict) and set(proof) == FIELDS and proof.get("schema") == SCHEMA and proof.get("issuer") == issuer() and proof.get("target") == selected and canonical.encode(proof) == raw, "native proof schema, issuer or canonical fields differ")
    metadata(proof)
    public = snapshot({"repository": repository, "marker": version, "run": "null"})
    require(proof["source"] == index["source"] == public["source"] and proof["publisher"] == public["publisher"] and proof["seal"] == public["seal_ref"], "native proof publication identity changed")
    require(proof["resolution"] == canonical.digest(plan.lineage(dict(public["distribution"]["resolution"]["binaries"], guard={}))), "native effective resolution changed")
    model = chain.model(fetch(f"https://raw.githubusercontent.com/{repository}/{public['source']['commit']}/plumb.toml"))
    require(model == proof["declaration"], "native proof full declaration differs from source")
    require(proof["managers"] == managers.verify(public, model), "native manager facts differ")
    held = public["seal"].get("artifacts", {}).get(specification["key"])
    require(held == proof["archive"], "native installation archive differs from seal")
    files = archive.unpack(reference(held, authority), model["targets"][selected]["installed"], selected)
    require(proof["files"] == {name: entry(body) for name, body in files.items()} and set(proof["binaries"]) == set(model["targets"][selected]["built"]), "native public file or complete build inventory differs")
    normalized = {}
    for name, body in files.items():
        logical = name.removesuffix(".exe") if specification["unit"] == "windows" else name
        original, facts = binary.inspect(body, (selected, public["source"], proof["build"]["key"]), logical)
        require(facts == proof["binaries"].get(logical), "native public executable facts differ")
        normalized[logical] = original
    require(fetch(url) == index_raw and snapshot({"repository": repository, "marker": version, "run": "null"}) == public, "native discovery or publication changed during verification")
    return proof, normalized


def associated(left, right, before, after):
    require(left["target"] == right["target"] and all(left["source"][name] == right["source"][name] for name in ("repository", "commit", "tree")), "native association source or target changed")
    require(left["declaration"] == right["declaration"] and left["resolution"] == right["resolution"], "native declaration or effective resolution changed")
    require(left["build"] == right["build"], "native effective build or recorded content changed")
    require(set(left["binaries"]) == set(right["binaries"]) and all({name: value[name] for name in ("origin", "normalized", "signature")} == {name: right["binaries"][file][name] for name in ("origin", "normalized", "signature")} for file, value in left["binaries"].items()), "native complete executable content changed")
    require(before == after and set(before) == set(left["declaration"]["targets"][left["target"]]["installed"]), "native complete installed bytes changed")
    managers.associated(left["managers"], right["managers"])


def check(held):
    public = snapshot(dict(held, run="null"))
    model = chain.model(fetch(f"https://raw.githubusercontent.com/{held['repository']}/{public['source']['commit']}/plumb.toml"))
    results = {}
    for selected in model["targets"]:
        if selected in TARGETS and model["targets"][selected]["installed"]:
            proof, _ = read(held["repository"], held["marker"], selected)
            results[selected] = {"proof": canonical.digest(proof), "archive": proof["archive"], "managers": proof["managers"]}
    require(results, "publication declares no supported native target")
    return {"schema": "wharf.binary.native.readback/v2", "source": public["source"], "targets": results, "unassessed": ["linux", "oci", "chart", "cargo", "npm", "cfworker", "deb"], "promotion": False}


def compare(held):
    require(marker.channel(held["reference"]) in ("alpha", "beta", "rc") and marker.channel(held["marker"]) == "stable", "native comparison needs non-stable and stable markers")
    checked = check(held)
    results = {}
    for selected in checked["targets"]:
        left, before = read(held["repository"], held["reference"], selected)
        right, after = read(held["repository"], held["marker"], selected)
        associated(left, right, before, after)
        results[selected] = {"proofs": [canonical.digest(left), canonical.digest(right)], "associated": True, "managers": right["managers"]}
    result = {"schema": "wharf.binary.native.comparison/v2", "associated": True, "targets": results, "source": checked["source"], "reference": held["reference"], "promotion": False}
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "comparison.json").write_bytes(canonical.encode(result))
    return result


def metadata(proof):
    for name in ("build", "binding"):
        held = proof[name]
        require(isinstance(held, dict) and set(held) == {"key", "basis", "implementation", "record", "producer"} and all(digest(held[field]) for field in ("key", "basis", "implementation", "record")), "native workload summary fields differ")
        producer = held["producer"]
        require(isinstance(producer, dict) and set(producer) == {"repository", "marker", "wharf", "run", "attempt"} and producer["repository"] == proof["source"]["repository"], "native workload producer fields differ")
        control.location(producer["repository"], producer["marker"])
        control.trusted(control.observer({field: producer[field] for field in ("wharf", "run", "attempt")}))
    for value in proof["binaries"].values():
        require(isinstance(value, dict) and set(value) == {"origin", "bound", "normalized", "signature"} and value["origin"] == {"prefix": proof["source"]["repository"].split("/")[1].upper().replace("-", "_"), "commit": "", "target": proof["target"]} and value["signature"] == target(proof["target"])["signature"], "native complete binary facts differ")
        require(all(isinstance(value[field], dict) and set(value[field]) == {"size", "sha256"} and type(value[field]["size"]) is int and 0 < value[field]["size"] <= LIMIT and digest(value[field]["sha256"]) for field in ("bound", "normalized")), "native binary fact bounds differ")
    held = proof["evidence"]
    require(isinstance(held, dict) and set(held) == {"key", "record", "verification"} and digest(held["key"]) and digest(held["record"]), "native material reference fields differ")
    require(isinstance(held["verification"], dict) and set(held["verification"]) == {"size", "sha256"} and type(held["verification"]["size"]) is int and 0 < held["verification"]["size"] <= 4194304 and digest(held["verification"]["sha256"]), "native verification material bounds differ")
