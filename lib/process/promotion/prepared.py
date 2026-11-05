import json
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical, implementation, resources
from lib.identity.bind import inventory, storage, verification
from lib.identity.bind.spec import entry, require
from lib.process.association import chain
from lib.process.promotion import preflight
from lib.store import plan, r2, workload

SCHEMA = "wharf.ship.prepared/v1"
MATERIAL = "wharf.ship.prepared.material/v1"
BUILD = resources.read_json("build/config.json")
FORBIDDEN = {"[record] distribution", "[prove] published binary", "[prove] native binary", "[windtun] prepare attempt", "[windtun] issue qualification", "[windtun] prepare promotion", "[windtun] published artifacts", "[publish] npm", "[publish] oci", "[publish] chart", "[publish] binaries", "[publish] cargo", "[point] channel", "[deploy] cfworker"}


def code():
    return implementation.resourced(["lib.process.promotion.prepared"], ["build/config.json", "identity/format.json", "manager/association.json", "releases.json", "validators.json", "manager/unix.sh.in", "manager/windows.ps1.in"])


def jobs(rows):
    require(all(sum(row.get("name") == name for row in rows) == 1 for name in FORBIDDEN), "preparation publication role inventory is incomplete or ambiguous")
    for row in rows:
        if row["name"] in FORBIDDEN or row["name"].startswith(("[publish]", "[deploy]", "[point]")):
            require(row.get("status") == "completed" and row.get("conclusion") == "skipped" and not row.get("steps"), "preparation reached a publication role")
    for name in ("[plan] entries",):
        selected = [row for row in rows if row["name"] == name]
        require(len(selected) == 1 and selected[0].get("status") == "completed" and selected[0].get("conclusion") == "success", "preparation source plan did not complete")


def smoke(bucket, document, names, target):
    name = "smoke-" + target["name"]
    loaded = storage.read(bucket, document["entries"][name]["key"], implementation.resourced(["lib.identity.smoke"], []))
    bound = document["entries"]["bind-" + target["name"]]["key"]
    require(loaded["basis"] == {"entry": {"kind": "binary-smoke", "binary": bound}} and set(loaded["files"]) == {"receipt.json"}, "native smoke basis or file coverage differs")
    storage.producer(bucket, loaded, name)
    receipt = loaded["receipt"]
    require(receipt.get("action") == "ship.binary.smoke" and receipt.get("marker") == document["context"]["marker"], "native smoke receipt names another marker")
    suffix = ".exe" if target["name"] == "windows" else ""
    expected = {value: value + "-" + target["target"] + suffix for value in names}
    rows = receipt.get("binaries")
    require(isinstance(rows, list) and len(rows) == len(names) and [row.get("file") for row in rows] == [expected[value] for value in sorted(names)], "native smoke complete binary inventory differs")
    for row, value in zip(rows, sorted(names)):
        require(row.get("expect") == value + " " + document["context"]["marker"] and set(row.get("surfaces", {})) == {"--version", "--help"} and row["surfaces"]["--version"] == row["expect"] and isinstance(row["surfaces"]["--help"], str) and row["surfaces"]["--help"], "native smoke surfaces are incomplete")
    return storage.summary(loaded)


def complete(bucket, document, source):
    model = chain.frozen(document, source)
    selected = [item for item in BUILD["targets"] if item["target"] in model["targets"]]
    require(selected and model["targets"], "preparation has no declared binary targets")
    expected = {prefix + item["name"] for item in selected for prefix in ("binary-", "dependencies-", "bind-", "smoke-")} | {"validate"}
    require(set(document["entries"]) == expected, "preparation plan has missing or non-preparation entries")
    results = {}
    for target in selected:
        if target["name"] == BUILD["primary"]:
            facts = verification.chain(bucket, source, document, inventory.planned(document["context"], source))
        else:
            facts = chain.verify(bucket, source, document, (model, target["target"]))
        results[target["target"]] = {"build": storage.summary(facts["build"]), "binding": storage.summary(facts["binding"]), "smoke": smoke(bucket, document, model["targets"][target["target"]]["built"], target)}
    loaded = storage.read(bucket, document["entries"]["validate"]["key"], implementation.resourced(["lib.identity.smoke"], ["validators.json"]))
    require(loaded["basis"] == {"entry": {"kind": "binary-validate", "binary": document["entries"]["bind-" + BUILD["primary"]]["key"]}} and set(loaded["files"]) == {"receipt.json"}, "preparation validator basis differs")
    storage.producer(bucket, loaded, "validate")
    expected_steps = [" ".join(step) for step in resources.read_json("validators.json").get(source["repository"], [])]
    require(loaded["receipt"] == {"action": "ship.binary.configured", "steps": expected_steps}, "preparation validator receipt differs")
    return {"declaration": model, "targets": results, "validation": storage.summary(loaded)}


def assess(held):
    observer, rows = preflight.active(held["observer"], "prepared")
    jobs(rows)
    actual = preflight.event()
    require(actual["prepare"] and all(actual[name] == held[name] for name in ("repository", "marker")) and held["run"] == observer["run"], "prepared observation belongs to another dispatch")
    bucket = r2.configured()
    document, reference = verification.planned(bucket, {"repository": held["repository"], "marker": held["marker"], "run": held["run"], "attempt": held["planned"]})
    require(reference["wharf"] == observer["wharf"] and held["planned"] == observer["attempt"], "prepared observation belongs to another source or plan attempt")
    context = document["context"]
    source = {name: context[name] for name in ("repository", "marker", "commit", "tree")}
    intent = preflight.confirm(json.loads(held["preparation"]), source)
    require(not bucket.exists(plan.publication(context)), "preparation has an existing product publication reservation")
    facts = complete(bucket, document, source)
    preflight.confirm(intent, source)
    _, current = preflight.active(held["observer"], "prepared")
    jobs(current)
    require(not bucket.exists(plan.publication(context)), "publication began during preparation")
    return {"schema": SCHEMA, "state": "prepared", "observer": observer, "source": source, "intent": intent, "plan": reference, **facts, "unassessed": ["published-stable", "cargo", "npm", "oci", "chart", "cfworker", "deb"], "publication": False, "promotion": False}


def produce(held):
    result = assess(held)
    basis = {"schema": MATERIAL, "observer": result["observer"], "plan": result["plan"], "intent": result["intent"]}
    key = workload.key(basis, code())
    body = canonical.encode(result)
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "prepared.json").write_bytes(body)
        workload.publish(r2.configured(), key, workload.Produced(Path(directory), basis, {"observer": result["observer"]}))
    require(r2.configured().get(workload.prefix(key) + "blobs/prepared.json") == body, "prepared observation readback differs")
    parameters.answer({"digest": key, "ready": "true"})
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "prepared.json").write_bytes(body)
    return dict(result, evidence={"key": key, "sha256": canonical.digest(result)})


def consume(held):
    result = assess(held)
    basis = {"schema": MATERIAL, "observer": result["observer"], "plan": result["plan"], "intent": result["intent"]}
    require(workload.key(basis, code()) == held["digest"], "prepared material belongs to another observation")
    bucket = r2.configured()
    restore(bucket, held["digest"], (basis, result))
    (Path(held["source"]) / "readback.json").write_bytes(canonical.encode(result))
    return result


def restore(bucket, key, expected):
    basis, result = expected
    prefix = workload.prefix(key)
    raw_basis, raw_record = bucket.get(prefix + "basis.json"), bucket.get(prefix + "record.json")
    require(len(raw_basis) <= 1048576 and len(raw_record) <= 1048576, "prepared metadata exceeds bounds")
    record = json.loads(raw_record)
    require(raw_basis == canonical.encode(basis) and raw_record == canonical.encode(record), "prepared metadata differs or is noncanonical")
    require(set(record) == {"hash_version", "key", "context", "files"} and type(record["hash_version"]) is int and record["hash_version"] == 1 and record["key"] == key and record["context"] == {"observer": result["observer"]}, "prepared record identity differs")
    body = canonical.encode(result)
    require(len(body) <= 4194304 and record["files"] == {"prepared.json": entry(body)}, "prepared inventory differs from independent verification")
    require(bucket.get(prefix + "blobs/prepared.json") == body, "prepared bytes differ from independent verification")
