import re
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.identity.bind.spec import entry, require
from lib.media import release
from lib.process.promotion import capture, preflight, reader
from lib.refusal import Conflict
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


def facts(arguments):
    status, report = reader.capture(arguments)
    require(status == 0 and isinstance(report, dict) and set(report) == {"state", "facts", "publication"}, "shared semantic reader did not return bounded facts")
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
