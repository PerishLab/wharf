from pathlib import Path

from lib.content import canonical, marker
from lib.identity.bind import control
from lib.identity.bind.spec import digest
from lib.media import release
from lib.process.association import archive, binary, chain, managers
from lib.process.association.spec import DISCOVERY, LIMIT, SCHEMA, TARGETS, document, entry, fetch, issuer, require, sha, target, Unavailable
from lib.store import plan
from lib.refusal import Refusal

FIELDS = {"schema", "issuer", "source", "publisher", "resolution", "target", "declaration", "seal", "archive", "files", "binaries", "build", "binding", "evidence", "managers"}


def fetched(url, stage, limit=1048576):
    try:
        return fetch(url, limit)
    except Unavailable as failure:
        raise failure.during(stage=stage) from None


def managed(publication, model):
    try:
        return managers.verify(publication, model)
    except Unavailable as failure:
        raise failure.during(stage="manager") from None


def snapshot(held):
    _, authority, root = control.location(held["repository"], held["marker"])
    record = document(fetched(f"{authority}/{root}/distribution.json", "distribution"))
    require(type(record.get("schema")) is int and record["schema"] == 1, "native distribution schema differs")
    source = control.source({name: record.get(name) for name in control.FIELDS})
    require(source["repository"] == held["repository"] and source["marker"] == held["marker"] and record.get("state") == "complete" and record.get("media", {}).get("binaries") in ("published", "present"), "native association needs the exact complete binary distribution")
    publisher = control.observer(record.get("completed"))
    require(held["run"] == "null" or held["run"] == publisher["run"], "native publication run differs from request")
    control.trusted(publisher, "publisher")
    raw = fetched(f"{authority}/{root}/seal.json", "seal")
    seal = document(raw)
    require(type(seal.get("schema")) is int and seal["schema"] == 1 and all(seal.get(name) == value for name, value in (("product", source["repository"].split("/")[1]), ("releaseVersion", source["marker"]), ("channel", marker.channel(source["marker"])), ("commit", source["commit"]))), "native public seal identity differs")
    return {"source": source, "publisher": publisher, "distribution": record, "seal": seal, "seal_ref": release.remote(authority, root + "/seal.json", raw, release.JSON)}


def reference(value, authority, stage="material"):
    require(isinstance(value, dict) and set(value) == {"url", "name", "mime", "size", "sha256"} and type(value["size"]) is int and 0 < value["size"] <= 2 * LIMIT and digest(value["sha256"]), "native object reference fields or bounds differ")
    require(isinstance(value["name"], str) and value["name"] not in ("", ".", "..") and "/" not in value["name"] and "\\" not in value["name"] and value["url"] == f"{authority}/v1/objects/sha256/{value['sha256']}/{value['name']}", "native object reference is foreign")
    raw = fetched(value["url"], stage, value["size"])
    require(entry(raw) == {name: value[name] for name in ("size", "sha256")}, "native object bytes differ from reference")
    return raw


def read(repository, version, selected):
    target(selected)
    control.location(repository, version)
    try:
        return verified(repository, version, selected)
    except Unavailable as failure:
        raise failure.during(selected=selected, version=version) from None


def verified(repository, version, selected):
    specification = target(selected)
    _, authority, root = control.location(repository, version)
    url = f"{authority}/{root}/build/native-{specification['key']}.json"
    index_raw = fetched(url, "discovery")
    index = document(index_raw)
    require(isinstance(index, dict) and set(index) == {"schema", "source", "target", "authority", "observer", "started", "proof"} and index["schema"] == DISCOVERY and index["target"] == selected and index["authority"] == authority, "native discovery fields or target differ")
    control.source(index["source"])
    require(index["source"]["repository"] == repository and index["source"]["marker"] == version, "native discovery belongs to another publication")
    control.trusted(control.observer(index["observer"]), "issuer")
    control.started(index["started"])
    require(isinstance(index["proof"], dict) and type(index["proof"].get("size")) is int and index["proof"]["size"] <= 4194304, "native proof exceeds document bounds")
    raw = reference(index["proof"], authority, "proof")
    require(len(raw) <= 4194304 and index["proof"]["name"] == "native-association.json", "native proof is outside policy bounds")
    proof = document(raw)
    require(isinstance(proof, dict) and set(proof) == FIELDS and proof.get("schema") == SCHEMA and proof.get("issuer") == issuer() and proof.get("target") == selected and canonical.encode(proof) == raw, "native proof schema, issuer or canonical fields differ")
    metadata(proof)
    public = snapshot({"repository": repository, "marker": version, "run": "null"})
    require(proof["source"] == index["source"] == public["source"] and proof["publisher"] == public["publisher"] and proof["seal"] == public["seal_ref"], "native proof publication identity changed")
    require(proof["resolution"] == canonical.digest(plan.lineage(dict(public["distribution"]["resolution"]["binaries"], guard={}))), "native effective resolution changed")
    model = chain.model(fetched(f"https://raw.githubusercontent.com/{repository}/{public['source']['commit']}/plumb.toml", "declaration"))
    require(model == proof["declaration"], "native proof full declaration differs from source")
    require(proof["managers"] == managed(public, model), "native manager facts differ")
    held = public["seal"].get("artifacts", {}).get(specification["key"])
    require(held == proof["archive"], "native installation archive differs from seal")
    files = archive.unpack(reference(held, authority, "archive"), model["targets"][selected]["installed"], selected)
    require(proof["files"] == {name: entry(body) for name, body in files.items()} and set(proof["binaries"]) == set(model["targets"][selected]["built"]), "native public file or complete build inventory differs")
    normalized = {}
    for name, body in files.items():
        logical = name.removesuffix(".exe") if specification["unit"] == "windows" else name
        original, facts = binary.inspect(body, (selected, public["source"], proof["build"]["key"]), logical)
        require(facts == proof["binaries"].get(logical), "native public executable facts differ")
        normalized[logical] = original
    require(fetched(url, "discovery") == index_raw and snapshot({"repository": repository, "marker": version, "run": "null"}) == public, "native discovery or publication changed during verification")
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
    model = chain.model(fetched(f"https://raw.githubusercontent.com/{held['repository']}/{public['source']['commit']}/plumb.toml", "declaration"))
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


def observe(action, held, value):
    location = held.get("observation", "null")
    if location == "null":
        return
    request = {}
    for name in ("marker", "reference"):
        version = held.get(name)
        request[name] = version if isinstance(version, str) and len(version) <= 160 and marker.holds(version) else None
    try:
        control.location(held.get("repository"), request["marker"])
        request["repository"] = held["repository"]
    except (Refusal, TypeError, AttributeError):
        request["repository"] = None
    run = held.get("run", "null")
    request["run"] = run if isinstance(run, str) and run.isascii() and run.isdecimal() and 0 < len(run) <= 20 and int(run) > 0 else None
    try:
        request["observer"] = control.observer(held["observer"]) if "observer" in held else None
    except (Refusal, ValueError, TypeError):
        request["observer"] = None
    result = {"schema": "wharf.binary.native.observation/v1", "action": action, "request": request, "state": "succeeded", "associated": value.get("associated") if isinstance(value, dict) else None, "promotion": False}
    if isinstance(value, Unavailable):
        failure = value.during(version=request["marker"]) if value.details["marker"] is None else value
        result.update(state="unknown", failure={"kind": failure.kind, **failure.details})
    elif isinstance(value, Refusal):
        result.update(state="refused", failure={"kind": "verification"})
    require(action in ("native-proof", "native-read", "native-compare"), "native observation action is unsupported")
    raw = canonical.encode(result)
    require(len(raw) <= 4096, "native observation exceeds bounds")
    try:
        root = Path(location).resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = root / (action + ".json")
        try:
            with destination.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            require(destination.stat().st_size == len(raw) and destination.read_bytes() == raw, "native observation already differs")
    except OSError:
        raise Refusal("native observation could not be retained") from None
