import re

from lib import parameters
from lib.content import canonical
from lib.identity.bind.spec import require
from lib.media import release
from lib.process.promotion import capture, preflight, reader
from lib.process.confirmation import semantics
from lib.store import r2

HEAD = "v1/semantics/family/latest.json"
FIRST = "v1/semantics/family/initial.json"
SCHEMA = "wharf.release.semantics.family.head/v1"


def requested(held):
    native = capture.active(held, "semantics-select")
    selected = preflight.semantics(preflight.event()["semantics"])
    require(selected["kind"] == "selection", "family selector needs its exact native request")
    record = dict(selected["record"], native=native)
    require(record["product"] == held["repository"].split("/")[1] and record["marker"] == held["marker"], "family selection names another actual product or marker")
    return native, selected, record


def materials(root, record):
    arguments = ["release", "inspect", "--input", str(root / "record.json"), "--purpose", "nomination", "--product", record["product"]]
    status, result = reader.select(arguments)
    report = semantics.observed(status, result, arguments)
    require(set(report) == {"schema", "selection", "family", "history", "predecessors", "inception", "authentication", "publication"} and report["schema"] == "plumb.release.selection.materials/v1", "shared family selection material protocol differs")
    require(report["selection"] == record and report["authentication"] is False and report["publication"] is False, "selection preparation claims issuer authority or another actual record")
    require(isinstance(report["family"], dict) and isinstance(report["history"], dict) and isinstance(report["predecessors"], list), "selection lacks its family, retained history or predecessor facts")
    for name in ("family", "history"):
        require(report[name].get("authentication") is True and report[name].get("publication") is False, "selection needs authenticated family and history observations")
    require(report["family"].get("root") == record["root"] and report["family"].get("reference") == record["family"] and report["history"].get("reference") == record["history"], "selection material references differ")
    source = report["family"].get("stable", {}).get("world", {}).get("source", {})
    require(source.get("repository") == f"PerishLab/{record['product']}" and source.get("marker") == record["marker"] and isinstance(source.get("commit"), str) and re.fullmatch(r"[0-9a-f]{40}", source["commit"]), "selection material has no exact formal source")
    return report


def protected(bucket, record, report):
    prior, etag = bucket.snapshot(HEAD)
    first, first_etag = bucket.snapshot(FIRST)
    expected = None if record["previous"] is None else canonical.encode({"schema": SCHEMA, "reference": record["previous"]})
    require(prior == expected and (prior is None) == (etag is None), "protected family head differs from exact predecessor")
    if record["previous"] is None:
        require(first is None and first_etag is None and report["inception"] is None, "family selection is already initialized; missing discovery cannot reset inception")
    else:
        require(isinstance(report["inception"], dict) and first == canonical.encode(report["inception"]) and first_etag is not None, "protected family inception differs")
    return prior, etag, first


def prepare(held):
    root = semantics.destination(held, False)
    native, selected, record = requested(held)
    root.mkdir(parents=True)
    for name, value in {"record": record, "request": selected}.items():
        (root / f"{name}.json").write_bytes(canonical.encode(value))
    authority = reader.identity()
    report = materials(root, record)
    require(reader.identity() == authority and requested(held) == (native, selected, record), "selection preparation reader or actual context moved")
    result = {"schema": "wharf.release.semantics.selection.preparation/v1", "native": native, "request": selected, "reference": semantics.object_reference(record["product"], canonical.encode(record)), "reader": authority, "materials": report, "authentication": False, "publication": False}
    (root / "preparation.json").write_bytes(canonical.encode(result))
    return result


def prepared(held):
    root = semantics.destination(held, True)
    receipts(root, {"record.json", "request.json", "preparation.json"})
    native, selected, record = requested(held)
    require((root / "record.json").read_bytes() == canonical.encode(record) and (root / "request.json").read_bytes() == canonical.encode(selected), "selection preparation differs from actual request")
    result = capture.decode((root / "preparation.json").read_bytes())
    require(set(result) == {"schema", "native", "request", "reference", "reader", "materials", "authentication", "publication"} and result["schema"] == "wharf.release.semantics.selection.preparation/v1", "selection preparation protocol differs")
    require(result["native"] == native and result["request"] == selected and result["reference"] == semantics.object_reference(record["product"], canonical.encode(record)) and result["reader"] == reader.identity() and result["authentication"] is False and result["publication"] is False, "selection preparation differs from actual native or formal reader")
    return root, result, record


def produce(held):
    root, preparation, record = prepared(held)
    native, selected, authority = preparation["native"], preparation["request"], preparation["reader"]
    report = materials(root, record)
    require(report == preparation["materials"], "selection materials moved since preparation")
    raw = canonical.encode(record)
    reference = preparation["reference"]
    selector = {"schema": SCHEMA, "reference": reference}
    source = report["family"]["stable"]["world"]["source"]
    _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], source["commit"], native["commit"]))
    publisher = r2.writer(bucket, "RELEASES")
    prior, etag, first = protected(publisher, record, report)
    intent = {"schema": "wharf.release.semantics.selection.intent/v1", "native": native, "request": selected, "reference": reference, "reader": authority, "etag": etag, "previous": None if prior is None else capture.decode(prior), "inception": None if first is None else capture.decode(first), "authentication": False, "publication": False}
    for name, value in {"intent": intent, "materials": report, "reference": reference}.items():
        (root / f"{name}.json").write_bytes(canonical.encode(value))
    require(reader.identity() == authority and requested(held) == (native, selected, record) and protected(publisher, record, report) == (prior, etag, first), "selection reader, predecessor or native context moved before retention")
    require(semantics.retain(publisher, record["product"], raw) == reference, "selection retained another record")
    if record["previous"] is None:
        publisher.create(FIRST, canonical.encode(selector), {"Content-Type": release.JSON})
        require(publisher.snapshot(FIRST)[0] == canonical.encode(selector), "selection inception readback differs")
    else:
        require(publisher.snapshot(FIRST)[0] == first, "selection inception moved before update")
    require(reader.identity() == authority and capture.active(held, "semantics-select") == native, "selection reader or native context moved before head update")
    publisher.swap(HEAD, canonical.encode(selector), etag)
    require(publisher.snapshot(HEAD)[0] == canonical.encode(selector), "protected family head readback differs")
    require(release.fetch(f"https://releases.{record['product']}.perish.uk/{HEAD}") == canonical.encode(selector), "public family head readback differs")
    parameters.answer({"digest": reference["sha256"]})
    return intent


def receipts(root, names):
    require({path.name for path in root.iterdir()} == names and all(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= capture.LIMIT for path in root.iterdir()), "selection confirmation receipt set differs")


def confirm(held):
    root = semantics.destination(held, True)
    receipts(root, {"record.json", "request.json", "preparation.json", "intent.json", "materials.json", "reference.json"})
    native, selected, record = requested(held)
    raw = (root / "record.json").read_bytes()
    require(raw == canonical.encode(record) and capture.decode((root / "request.json").read_bytes()) == selected, "selection receipt differs from actual request")
    reference = semantics.object_reference(record["product"], raw)
    intent = capture.decode((root / "intent.json").read_bytes())
    require(reference == capture.decode((root / "reference.json").read_bytes()) == intent.get("reference") and held["digest"] == reference["sha256"], "selection confirmation digest differs")
    require(intent.get("native") == native and intent.get("request") == selected and intent.get("reader") == reader.identity() and intent.get("authentication") is False and intent.get("publication") is False, "selection intent exceeds its actual native context")
    selector = {"schema": SCHEMA, "reference": reference}
    first = selector if record["previous"] is None else intent["inception"]
    expected = capture.decode((root / "materials.json").read_bytes())
    expected["inception"] = first
    require(materials(root, record) == expected, "selection current material facts moved before confirmation")
    authority = f"https://releases.{record['product']}.perish.uk/"
    require(release.fetch(reference["url"]) == raw and release.fetch(authority + HEAD) == canonical.encode(selector) and release.fetch(authority + FIRST) == canonical.encode(first), "public selection, head or inception differs")
    require(reader.identity() == intent["reader"] and capture.active(held, "semantics-select") == native, "selection final reader or context moved")
    result = {"schema": "wharf.release.semantics.selection.confirmation/v1", "reference": reference, "native": native, "authentication": False, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result
