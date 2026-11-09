from lib import parameters
from lib.content import canonical
from lib.identity.bind.spec import require
from lib.media import release
from lib.package.source import reconstruct
from lib.process.confirmation import semantics
from lib.process.promotion import capture, preflight, reader
from lib.store import plan, r2


def request(held):
    native = capture.active(held, "semantics-original")
    selected = preflight.semantics(preflight.event()["semantics"])
    require(selected["kind"] == "original", "original source needs its actual native material request")
    return native, selected


def receipts(root, names):
    require({p.name for p in root.iterdir()} == names and all(p.is_file() and not p.is_symlink() and 0 < p.stat().st_size <= capture.LIMIT for p in root.iterdir()), "original source receipt set differs or exceeds bounds")


def observe(held, root):
    native, selected = request(held)
    distribution, reference = capture.published(held)
    document = capture.decode((root / "plan.json").read_bytes())
    require(type(document.get("schema")) is int and document["schema"] == plan.SCHEMA and plan.reference(document) == distribution["plan"], "original saved plan differs from exact public reference")
    capture.original_native(distribution["plan"])
    world, raw = capture.snapshot(distribution, document)
    require(raw == (root / "world.json").read_bytes(), "original frozen world receipt moved")
    material = reconstruct.recover(world, selected["recovery"], document["context"].get("material"))
    require(canonical.encode(material) == (root / "source.json").read_bytes(), "original reconstructed source receipt moved")
    authority, inspection = capture.inspect(world, raw)
    require(request(held) == (native, selected) and capture.published(held) == (distribution, reference), "original request or publication moved during observation")
    return {"native": native, "request": selected, "original": {"distribution": reference, "plan": distribution["plan"]}, "reader": authority, "world": world, "inspection": inspection, "material_origin": "recorded" if document["context"].get("material") is not None else "recovered" if selected["recovery"] else "archive"}


def prepare(held):
    root = semantics.destination(held, False)
    native, selected = request(held)
    distribution, reference = capture.published(held)
    document = capture.original(distribution["plan"])
    world, raw = capture.snapshot(distribution, document)
    material = reconstruct.recover(world, selected["recovery"], document["context"].get("material"))
    root.mkdir(parents=True)
    for name, value in {"plan": document, "world": world, "source": material}.items():
        body = canonical.encode(value)
        require(0 < len(body) <= capture.LIMIT, "original material receipt exceeds bounds")
        (root / f"{name}.json").write_bytes(body)
    observed = observe(held, root)
    require(observed["native"] == native and observed["request"] == selected and observed["original"] == {"distribution": reference, "plan": distribution["plan"]}, "original prepared context moved")
    (root / "preparation.json").write_bytes(canonical.encode(observed))
    return observed


def produce(held):
    root = semantics.destination(held, True)
    receipts(root, {"plan.json", "world.json", "source.json", "preparation.json"})
    observed = observe(held, root)
    require(observed == capture.decode((root / "preparation.json").read_bytes()), "original prepared material facts moved")
    product = held["repository"].split("/")[1]
    record = {"schema": "wharf.release.semantics.original/v1", **observed, "world": semantics.object_reference(product, (root / "world.json").read_bytes()), "material": semantics.object_reference(product, (root / "source.json").read_bytes()), "authentication": False, "qualification": False, "activation": False, "publication": False}
    raw = canonical.encode(record)
    reference = semantics.object_reference(product, raw)
    intent = {"schema": "wharf.release.semantics.original.intent/v1", "record": record, "reference": reference, "authentication": False, "publication": False}
    for name, value in {"intent": intent, "record": record, "reference": reference}.items():
        (root / f"{name}.json").write_bytes(canonical.encode(value))
    source = observed["world"]["source"]
    _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], source["commit"], observed["native"]["commit"]))
    publisher = r2.writer(bucket, "RELEASES")
    require(reader.identity() == observed["reader"] and request(held) == (observed["native"], observed["request"]), "original formal reader or native context moved before retention")
    for field, name in (("world", "world.json"), ("material", "source.json")):
        require(semantics.retain(publisher, product, (root / name).read_bytes()) == record[field], "original retained material differs")
    require(semantics.retain(publisher, product, raw) == reference, "original retained descriptor differs")
    parameters.answer({"digest": reference["sha256"]})
    return intent


def confirm(held):
    root = semantics.destination(held, True)
    receipts(root, {"plan.json", "world.json", "source.json", "preparation.json", "intent.json", "record.json", "reference.json"})
    observed = observe(held, root)
    require(observed == capture.decode((root / "preparation.json").read_bytes()), "original material facts moved before confirmation")
    record = capture.decode((root / "record.json").read_bytes())
    reference = semantics.object_reference(held["repository"].split("/")[1], (root / "record.json").read_bytes())
    intent = capture.decode((root / "intent.json").read_bytes())
    require(intent == {"schema": "wharf.release.semantics.original.intent/v1", "record": record, "reference": reference, "authentication": False, "publication": False} and reference == capture.decode((root / "reference.json").read_bytes()) and held["digest"] == reference["sha256"], "original confirmation intent or digest differs")
    require(set(record) == {"schema", "native", "request", "original", "reader", "inspection", "material_origin", "world", "material", "authentication", "qualification", "activation", "publication"} and record["schema"] == "wharf.release.semantics.original/v1", "original descriptor fields differ")
    require({key: record[key] for key in observed if key != "world"} == {key: value for key, value in observed.items() if key != "world"} and all(record.get(name) is False for name in ("authentication", "qualification", "activation", "publication")), "original descriptor context or authority differs")
    require(release.fetch(reference["url"]) == canonical.encode(record), "original descriptor public bytes differ")
    for field, name in (("world", "world.json"), ("material", "source.json")):
        require(record[field] == semantics.object_reference(held["repository"].split("/")[1], (root / name).read_bytes()) and release.fetch(record[field]["url"]) == (root / name).read_bytes(), "original source public bytes differ")
    require(reader.identity() == observed["reader"] and request(held) == (observed["native"], observed["request"]), "original reader or native context moved at confirmation")
    result = {"schema": "wharf.release.semantics.original.confirmation/v1", "reference": reference, "native": observed["native"], "authentication": False, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result
