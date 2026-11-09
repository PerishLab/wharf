import re
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.identity.bind.spec import entry, require
from lib.media import release
from lib.process.promotion import capture, preflight, reader
from lib.refusal import Conflict, Refusal
from lib.store import r2


def object_reference(product, raw):
    require(0 < len(raw) <= capture.LIMIT, "semantic object exceeds its material budget")
    held = entry(raw)
    return {"url": f"https://releases.{product}.perish.uk/v1/semantics/objects/{held['sha256']}.json", **held}


def retain(bucket, product, raw):
    reference = object_reference(product, raw)
    key = f"v1/semantics/objects/{reference['sha256']}.json"
    prior, _ = bucket.snapshot(key)
    if prior is None:
        try:
            bucket.create(key, raw, {"Content-Type": release.JSON})
        except Conflict:
            require(bucket.get(key) == raw, "semantic object address holds different bytes")
    else:
        require(prior == raw, "semantic object address holds different bytes")
    require(release.fetch(reference["url"]) == raw, "semantic object public readback differs")
    return reference


def facts(arguments, budget=180):
    status, report = reader.capture(arguments) if budget == 180 else reader.capture(arguments, budget)
    return observed(status, report, arguments)


def observed(status, report, arguments):
    require(isinstance(report, dict) and report.get("publication") is False, "shared semantic reader did not return bounded facts")
    if status != 0 or report.get("state") != "observed":
        reason = report.get("reason")
        require(report.get("state") in ("unknown", "refused") and isinstance(reason, str) and 0 < len(reason) <= 4096, "shared semantic reader failure has no bounded diagnosis")
        raise Refusal(f"shared semantic {arguments[1]} returned {report['state']} (exit {status}): {reason}")
    require(set(report) == {"state", "facts", "publication"}, "shared semantic fact fields differ")
    require(report.get("state") == "observed" and report.get("publication") is False and isinstance(report.get("facts"), dict), "shared semantic reader did not observe the required facts")
    return report["facts"]


def collected(source, selected):
    status, report = reader.capture(["release", "collect", "--repository", source["repository"], "--commit", source["commit"]])
    require(status == 0 and isinstance(report, dict) and report.get("schema") == "plumb.release.collection/v1" and report.get("state") == "observed", "shared collector did not observe current boundary materials")
    require(report.get("adoption") is False and report.get("publication") is False, "collection exceeds its observation role")
    snapshot = report.get("snapshot")
    observation = report.get("observation")
    require(isinstance(snapshot, dict) and isinstance(observation, dict), "collection has no complete source and Scene observations")
    expected = {name: source[name] for name in ("repository", "commit", "tree")}
    require(snapshot.get("source") == expected and observation.get("source") == expected, "collection names another original source")
    require(snapshot.get("product") == source["repository"].split("/")[1] and observation.get("scenes", {}).get("pointer") == selected, "collection names another current product or Scene")
    return report


def reviewed(product, location):
    report = facts(["release", "review", "--product", product, "--reference", location, "--purpose", "adoption"])
    require(report.get("reference", {}).get("url") == location and report.get("judgment", {}).get("product") == product, "shared adoption review names another product or reference")
    return report


def materials(path, record, distribution):
    report = facts(["release", "inspect", "--input", str(path), "--purpose", "materials", "--product", record["product"]])
    require(set(report) == {"schema", "data", "authentication", "publication"} and report.get("schema") == "plumb.release.materials/v1", "shared adoption material protocol differs")
    require(report.get("authentication") is False and report.get("publication") is False, "prepared material facts exceed their role")
    data = report.get("data")
    require(isinstance(data, dict) and data.get("collection", {}).get("snapshot") == record["release"]["snapshot"], "shared adoption materials name another snapshot")
    require(data.get("review", {}).get("reference") == record["review"] and data.get("published", {}).get("distribution") == distribution, "shared material review or original publication differs")
    return report


def destination(held, existing):
    path = Path(held["source"])
    require(path.is_absolute() and not path.is_symlink() and not path.resolve().is_relative_to(Path.cwd().resolve()), "semantic output needs an owned path outside source")
    require(path.is_dir() if existing else not path.exists(), "semantic output presence differs from its operation")
    return path


def issue(held):
    root = destination(held, False)
    native = capture.active(held, "semantics-issue")
    requested = preflight.semantics(preflight.event()["semantics"])
    distribution, original = capture.published(held)
    world, raw = capture.snapshot(distribution, capture.original(distribution["plan"]))
    authority, inspection = capture.inspect(world, raw)
    collection = collected(world["source"], requested["scenes"])
    product = held["repository"].split("/")[1]
    review = reviewed(product, requested["review"])
    require(review["judgment"].get("marker") == held["marker"] and review["judgment"].get("world") == entry(raw)["sha256"], "adoption review names another stable or original world")
    _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], world["source"]["commit"], native["commit"]))
    publisher = r2.writer(bucket, "RELEASES")
    reference = retain(publisher, product, raw)
    record = {"schema": "wharf.release.semantics.adoption/v1", "product": product, "release": {"marker": held["marker"], "snapshot": collection["snapshot"]}, "scenes": requested["scenes"], "world": reference, "review": review["reference"], "native": native}
    root.mkdir(parents=True)
    (root / "record.json").write_bytes(canonical.encode(record))
    report = materials(root / "record.json", record, distribution)
    require(report["data"].get("world") == world, "shared adoption material world differs from original capture")
    require(capture.published(held) == (distribution, original) and reader.identity() == authority, "original publication or formal reader moved before issuance")
    stored = retain(publisher, product, canonical.encode(record))
    for name, value in {"world": world, "inspection": inspection, "materials": report, "reader": authority, "original": original, "reference": stored}.items():
        (root / f"{name}.json").write_bytes(canonical.encode(value))
    parameters.answer({"digest": stored["sha256"]})
    return {"schema": "wharf.release.semantics.pending/v1", "reference": stored, "native": native, "authentication": False, "publication": False}


def confirm(held):
    if preflight.semantics(preflight.event()["semantics"])["kind"] == "transition":
        return transition(held, True)
    root = destination(held, True)
    native = capture.active(held, "semantics-issue")
    requested = preflight.semantics(preflight.event()["semantics"])
    require(isinstance(held["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", held["digest"]), "semantic confirmation needs the exact issued digest")
    raw = (root / "record.json").read_bytes()
    record = capture.decode(raw)
    product = held["repository"].split("/")[1]
    reference = object_reference(product, raw)
    require(reference["sha256"] == held["digest"] and release.fetch(reference["url"]) == raw, "semantic confirmation public object bytes differ")
    require(record.get("native") == native and record.get("product") == product and record.get("release", {}).get("marker") == held["marker"], "semantic confirmation names another native issue or product")
    require(record.get("scenes") == requested["scenes"] and record.get("review", {}).get("url") == requested["review"], "semantic confirmation differs from the actual adoption request")
    distribution, original = capture.published(held)
    require(original == capture.decode((root / "original.json").read_bytes()), "original publication moved after issuance")
    require(collected(record["release"]["snapshot"]["source"] | {"marker": held["marker"]}, requested["scenes"])["snapshot"] == record["release"]["snapshot"], "current adoption materials moved after issuance")
    require(materials(root / "record.json", record, distribution) == capture.decode((root / "materials.json").read_bytes()), "adoption material verification moved after issuance")
    require(reader.identity() == capture.decode((root / "reader.json").read_bytes()), "formal semantic reader moved after issuance")
    result = {"schema": "wharf.release.semantics.confirmation/v1", "reference": reference, "native": native, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result


HEAD = "v1/semantics/history/latest.json"


def baseline(held, root):
    report = facts(["release", "inspect", "--input", str(root / "root.json"), "--purpose", "baseline", "--product", held["repository"].split("/")[1]])
    require(report.get("schema") == "plumb.release.baseline/v1" and report.get("authentication") is True and report.get("publication") is False, "history needs an authenticated original baseline")
    data = report.get("data")
    require(isinstance(data, dict) and data.get("origin", {}).get("reference") == capture.decode((root / "root.json").read_bytes()), "history baseline names another adoption")
    require(data.get("origin", {}).get("record", {}).get("release", {}).get("marker") == held["marker"], "history selector differs from its original stable")
    return report


def history(held, root, native):
    request = capture.decode((root / "request.json").read_bytes())
    selected = preflight.semantics(preflight.event()["semantics"])
    require(selected["kind"] == "history" and all(request.get(name) == selected[name] for name in ("root", "previous")) and request.get("native") == native, "history preparation differs from its exact native request")
    product = held["repository"].split("/")[1]
    report = facts(["release", "inspect", "--input", str(root / "request.json"), "--purpose", "retention", "--product", product])
    require(set(report) == {"schema", "data", "authentication", "publication"} and report["schema"] == "plumb.release.history.materials/v1", "shared history preparation protocol differs")
    require(report["authentication"] is False and report["publication"] is False, "history preparation claims issuer or publication authority")
    record = report["data"]
    require(isinstance(record, dict) and record.get("schema") == "wharf.release.semantics.history/v1" and record.get("product") == product, "shared history checkpoint names another product or schema")
    require(record.get("native") == native and all(record.get(name) == request[name] for name in ("root", "previous")), "shared checkpoint differs from exact native preparation")
    return report


def checkpoint(held):
    root = destination(held, False)
    native = capture.active(held, "semantics-history")
    selected = preflight.semantics(preflight.event()["semantics"])
    require(selected["kind"] == "history", "history issuance needs its fixed request")
    request = {"schema": "wharf.release.semantics.history.request/v1", "root": selected["root"], "previous": selected["previous"], "native": native}
    root.mkdir(parents=True)
    (root / "root.json").write_bytes(canonical.encode(request["root"]))
    (root / "request.json").write_bytes(canonical.encode(request))
    authority = reader.identity()
    adopted = baseline(held, root)
    report = history(held, root, native)
    record = report["data"]
    raw = canonical.encode(record)
    product = held["repository"].split("/")[1]
    reference = object_reference(product, raw)
    head = {"schema": "wharf.release.semantics.history.head/v1", "reference": reference}
    expected = None if request["previous"] is None else canonical.encode(dict(head, reference=request["previous"]))
    source = adopted["data"]["materials"]["world"]["source"]
    _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], source["commit"], native["commit"]))
    publisher = r2.writer(bucket, "RELEASES")
    prior, etag = publisher.snapshot(HEAD)
    require(prior == expected and (prior is None) == (etag is None), "protected history differs from exact public predecessor")
    intent = {"schema": "wharf.release.semantics.history.intent/v1", "native": native, "root": request["root"], "previous": request["previous"], "reference": reference, "head": head, "etag": etag, "reader": authority, "authentication": False, "publication": False}
    for name, value in {"baseline": adopted, "preparation": report, "intent": intent, "record": record, "reference": reference}.items():
        (root / f"{name}.json").write_bytes(canonical.encode(value))
    require(history(held, root, native) == report and reader.identity() == authority, "shared history preparation moved before retention")
    require(retain(publisher, product, raw) == reference, "retained checkpoint reference differs")
    require(capture.active(held, "semantics-history") == native and reader.identity() == authority, "history native context or formal reader moved before head update")
    publisher.swap(HEAD, canonical.encode(head), etag)
    require(publisher.snapshot(HEAD)[0] == canonical.encode(head), "protected history head readback differs")
    require(release.fetch(f"https://releases.{product}.perish.uk/{HEAD}") == canonical.encode(head), "public history head readback differs")
    parameters.answer({"digest": reference["sha256"]})
    return intent


def confirmed(held):
    root = destination(held, True)
    native = capture.active(held, "semantics-history")
    names = {"root.json", "request.json", "baseline.json", "preparation.json", "intent.json", "record.json", "reference.json"}
    require({path.name for path in root.iterdir()} == names and all((root / name).is_file() and not (root / name).is_symlink() for name in names), "history confirmation receipt file set differs")
    require(isinstance(held["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", held["digest"]), "history confirmation needs its exact digest")
    intent = capture.decode((root / "intent.json").read_bytes())
    request = capture.decode((root / "request.json").read_bytes())
    selected = preflight.semantics(preflight.event()["semantics"])
    require(selected["kind"] == "history" and all(selected[name] == request[name] == intent[name] for name in ("root", "previous")), "history confirmation names another actual request")
    raw = (root / "record.json").read_bytes()
    record = capture.decode(raw)
    product = held["repository"].split("/")[1]
    reference = object_reference(product, raw)
    require(reference == intent["reference"] == capture.decode((root / "reference.json").read_bytes()) and reference["sha256"] == held["digest"], "history confirmation reference differs")
    require(record.get("native") == request.get("native") == intent.get("native") == native and record.get("product") == product, "history confirmation names another native issuer or product")
    authority, inspection = capture.inspect(record, raw)
    require(authority == intent["reader"], "history data inspection or formal reader moved")
    baseline(held, root)
    require(release.fetch(reference["url"]) == raw and release.fetch(f"https://releases.{product}.perish.uk/{HEAD}") == canonical.encode(intent["head"]), "public history checkpoint or head moved before confirmation")
    require(intent.get("head") == {"schema": "wharf.release.semantics.history.head/v1", "reference": reference} and all(intent.get(name) is False for name in ("authentication", "publication")), "history receipt exceeds its retention role")
    result = {"schema": "wharf.release.semantics.history.confirmation/v1", "native": native, "reference": reference, "authentication": False, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result


def transition(held, existing=False):
    root = destination(held, existing)
    native = capture.active(held, "semantics-issue")
    requested = preflight.semantics(preflight.event()["semantics"])
    require(requested["kind"] == "transition", "transition issuer needs its exact native request")
    supplied = requested["record"]
    record = dict(supplied, intent=dict(supplied["intent"], native=native))
    product = held["repository"].split("/")[1]
    require(record["intent"]["product"] == product and record["intent"]["marker"] == held["marker"], "transition names another actual product or marker")
    if existing:
        require({path.name for path in root.iterdir()} == {"world.json", "record.json", "intent.json", "materials.json", "reference.json"} and all(not path.is_symlink() and path.is_file() and path.stat().st_size <= capture.LIMIT for path in root.iterdir()), "transition confirmation receipt set differs")
    distribution, original = capture.published(held)
    world, raw = (capture.decode((root / "world.json").read_bytes()), (root / "world.json").read_bytes()) if existing else capture.snapshot(distribution, capture.original(distribution["plan"]))
    require(record["intent"]["world"] == object_reference(product, raw), "transition world differs from original captured publication")
    require(world["source"] == {name: distribution[name] for name in ("repository", "marker", "commit", "tree")}, "transition original world names another source")
    encoded = canonical.encode(record)
    reference = object_reference(product, encoded)
    authority = reader.identity()
    intent = {"schema": "wharf.release.semantics.transition.intent/v1", "native": native, "request": requested, "reference": reference, "reader": authority, "original": original, "authentication": False, "publication": False}
    if existing:
        require(capture.decode((root / "intent.json").read_bytes()) == intent and (root / "record.json").read_bytes() == encoded, "transition confirmation differs from exact request or issuer")
        require(held["digest"] == reference["sha256"] and capture.decode((root / "reference.json").read_bytes()) == reference and release.fetch(reference["url"]) == encoded, "transition confirmation public bytes differ")
    else:
        root.mkdir(parents=True)
        for name, value in {"world": world, "record": record, "intent": intent}.items():
            (root / f"{name}.json").write_bytes(canonical.encode(value))
        _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], world["source"]["commit"], native["commit"]))
        publisher = r2.writer(bucket, "RELEASES")
        require(retain(publisher, product, raw) == record["intent"]["world"], "transition retained another original world")
    purpose = "transition" if requested["mode"] == "current" else "historical"
    report = facts(["release", "inspect", "--input", str(root / "record.json"), "--purpose", purpose, "--product", product], 900)
    require(set(report) == {"schema", "freshness", "previous", "derived", "authentication", "publication"} and report["schema"] == "plumb.release.transition.materials/v1" and report["freshness"] == requested["mode"], "shared transition material protocol or freshness differs")
    require(isinstance(report["previous"], dict) and isinstance(report["derived"], dict), "transition material families are absent")
    require(report["authentication"] is False and report["publication"] is False and report["previous"].get("authentication") is True and report["previous"].get("publication") is False and report["derived"].get("authentication") is False and report["derived"].get("publication") is False, "transition preparation exceeds its data role")
    require(report["previous"].get("reference") == record["intent"]["previous"] and report["derived"].get("reference") == reference, "shared transition derivation names another record or predecessor")
    if existing:
        require(capture.decode((root / "materials.json").read_bytes()) == report, "transition material facts moved after issuance")
    else:
        (root / "materials.json").write_bytes(canonical.encode(report))
    require(capture.published(held) == (distribution, original) and reader.identity() == authority and capture.active(held, "semantics-issue") == native, "transition publication, reader or native context moved")
    if existing:
        result = {"schema": "wharf.release.semantics.transition.confirmation/v1", "reference": reference, "native": native, "authentication": False, "publication": False}
        (root / "confirmation.json").write_bytes(canonical.encode(result))
        return result
    require(retain(publisher, product, encoded) == reference, "transition retained another record")
    (root / "reference.json").write_bytes(canonical.encode(reference))
    parameters.answer({"digest": reference["sha256"]})
    return intent
