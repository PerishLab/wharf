import io
import os
import re
import stat
import zipfile
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.identity.bind import control
from lib.identity.bind.spec import entry, require
from lib.identity.guard import pull
from lib.media import release
from lib.process.confirmation import semantics
from lib.process.promotion import capture, preflight, reader
from lib.refusal import Refusal
from lib.store import r2

FILES = {"selection.json", "collection.json", "world.json", "source.json", "inspection.json", "preparation.json", "confirmation.json"}


def unpack(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            require(set(archive.namelist()) == FILES and len(archive.infolist()) == len(FILES), "prepared world artifact file set differs")
            for info in archive.infolist():
                require(not info.is_dir() and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG), "prepared world artifact has a nonregular file")
                require(0 < info.file_size <= capture.LIMIT and not info.flag_bits & 1, "prepared world artifact file exceeds bounds or is encrypted")
            return {name: archive.read(name) for name in FILES}
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as error:
        raise Refusal("prepared world artifact is unreadable") from error


def job(actions, native, requested):
    route = f"/actions/runs/{native['run']}/attempts/{native['attempt']}"
    run = actions.get(route)
    require(isinstance(run, dict), "original world run metadata differs")
    preflight.native(run, {"run": str(native["run"]), "attempt": str(native["attempt"]), "wharf": native["commit"]})
    require(run.get("status") == "completed", "original world preparation run is incomplete")
    catalog = actions.get(route + "/jobs?per_page=100")
    require(isinstance(catalog, dict), "original world job catalog differs")
    jobs = catalog.get("jobs")
    require(isinstance(jobs, list) and type(catalog.get("total_count")) is int and 0 < len(jobs) <= 100 and len(jobs) == catalog["total_count"], "original world preparation jobs are incomplete")
    require(all(isinstance(value, dict) and all(type(value.get(name)) is int and value[name] > 0 for name in ("id", "run_id", "run_attempt")) for value in jobs), "original world job identities are invalid")
    require(len({value["id"] for value in jobs}) == len(jobs), "original world job identities repeat")
    require(all(value.get("run_id") == native["run"] and value.get("run_attempt") == native["attempt"] and value.get("head_sha") == native["commit"] and value.get("head_branch") == "main" for value in jobs), "original world jobs name another attempt")
    selected = [value for value in jobs if value.get("name") == preflight.ROLES["semantics-world"]]
    require(len(selected) == 1 and selected[0].get("id") == native["job"], "original semantic world role differs or is ambiguous")
    result = selected[0]
    require(result.get("status") == "completed" and result.get("conclusion") == "success", "original semantic world job did not succeed")
    steps = result.get("steps")
    require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps), "original world preparation steps differ")
    name = f"[prepare] world {requested['world']} {requested['material']}"
    selected = [step for step in steps if step.get("name") == name]
    require(len(selected) == 1 and selected[0].get("status") == "completed" and selected[0].get("conclusion") == "success", "original world step did not confirm both exact digests")
    return result


def artifact(actions, native):
    catalog = actions.get(f"/actions/runs/{native['run']}/artifacts?per_page=100")
    require(isinstance(catalog, dict), "original world artifact catalog differs")
    artifacts = catalog.get("artifacts")
    require(isinstance(artifacts, list) and type(catalog.get("total_count")) is int and 0 < len(artifacts) <= 100 and len(artifacts) == catalog["total_count"], "original world artifact inventory is incomplete")
    require(all(isinstance(value, dict) and type(value.get("id")) is int and value["id"] > 0 for value in artifacts), "original world artifact identities differ")
    require(len({value["id"] for value in artifacts}) == len(artifacts), "original world artifact identities repeat")
    selected = [value for value in artifacts if value.get("name") == f"semantics-world-{native['attempt']}"]
    require(len(selected) == 1 and selected[0].get("expired") is False, "original world artifact is absent, expired or ambiguous")
    held = selected[0]
    expected = {"id": native["run"], "repository_id": 1374358737, "head_repository_id": 1374358737, "head_branch": "main", "head_sha": native["commit"]}
    require(held.get("workflow_run") == expected, "original world artifact names another native producer")
    require(type(held.get("size_in_bytes")) is int and 0 < held["size_in_bytes"] <= pull.PULL["limit"], "original world artifact exceeds its transport budget")
    require(isinstance(held.get("digest"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", held["digest"]), "original world artifact has no exact provider digest")
    return held


def files(raw, requested):
    held = unpack(raw)
    for name, field in (("world.json", "world"), ("source.json", "material")):
        require(entry(held[name])["sha256"] == requested[field], "prepared world artifact differs from native confirmed bytes")
    native = requested["native"]
    selected = capture.decode(held["selection.json"])
    prepared = capture.decode(held["preparation.json"])
    confirmed = capture.decode(held["confirmation.json"])
    require(all(isinstance(value, dict) for value in (selected, prepared, confirmed)), "original world artifact receipts differ")
    require(selected.get("schema") == "wharf.release.semantics.selection/v1" and prepared.get("schema") == "wharf.release.semantics.preparation/v1" and confirmed.get("schema") == "wharf.release.semantics.confirmation/v1", "original world artifact preparation schemas differ")
    require(all(value.get("native") == native for value in (selected, prepared, confirmed)), "original world artifact names another preparation")
    require(all(prepared.get(name) == selected.get(name) for name in ("source", "scenes", "reader")), "original world preparation selections differ")
    require(all(value.get(name) is False for value in (prepared, confirmed) for name in ("adoption", "qualification", "activation", "publication")), "original prepared data exceeds its observation role")
    for field, name in (("world", "world.json"), ("material", "source.json"), ("collection", "collection.json")):
        reference = {"name": name, **entry(held[name])}
        require(prepared.get(field) == reference, "original prepared file reference differs")
        require(field == "collection" or confirmed.get(field) == reference, "original world confirmation reference differs")
    world = capture.decode(held["world.json"])
    material = capture.decode(held["source.json"])
    require(isinstance(world, dict) and isinstance(material, dict), "original prepared data is not an object")
    require(world.get("source") == material.get("source") == selected.get("source"), "original effective source names another selected product")
    require(material.get("schema") == "wharf.release.source/v1" and all(material.get(name) == world.get(name) for name in ("head", "tree")), "original effective source names another prepared world")
    return held


def captured(requested):
    preflight.semantics(canonical.encode(requested).decode())
    require(requested["kind"] == "retain", "world retention needs its fixed request")
    try:
        return read(requested)
    except pull.FAILURES as error:
        raise Refusal(f"original world observation is unreadable: {type(error).__name__}") from error


def read(requested):
    native = requested["native"]
    capture.lineage(native["commit"])
    actions = pull.Actions(os.environ.get("GH_TOKEN"), "PerishLab/wharf")
    original = job(actions, native, requested)
    reference = artifact(actions, native)
    raw = actions.archive(reference["id"])
    require(entry(raw) == {"sha256": reference["digest"].removeprefix("sha256:"), "size": reference["size_in_bytes"]}, "original world artifact bytes differ from provider digest or size")
    result = files(raw, requested)
    require(job(actions, native, requested) == original and artifact(actions, native) == reference, "original world preparation moved during observation")
    return result, reference


SCHEMA = "wharf.release.semantics.world/v1"


def destination(held, existing):
    path = Path(held["artifact"])
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), "world retention needs a canonical owned artifact path")
    require(not path.is_relative_to(Path.cwd().resolve()), "world retention artifacts must be outside source")
    require(path.is_dir() if existing else not path.exists(), "world retention artifact presence differs")
    return path


def observe(held):
    native = capture.active(held, "semantics-retain")
    requested = preflight.semantics(preflight.event()["semantics"])
    files, original = captured(requested)
    world = capture.decode(files["world.json"])
    source = control.source(world.get("source"))
    require(all(source[name] == held[name] for name in ("repository", "marker")), "retained world names another requested product or marker")
    authority, inspection = capture.inspect(world, files["world.json"])
    product = held["repository"].split("/")[1]
    record = {"schema": SCHEMA, "native": requested["native"], "world": semantics.object_reference(product, files["world.json"]), "material": semantics.object_reference(product, files["source.json"])}
    return {"native": native, "original": original, "world": world, "files": files, "reader": authority, "inspection": inspection, "record": record}


def produce(held):
    root = destination(held, False)
    observed = observe(held)
    record = observed["record"]
    raw = canonical.encode(record)
    product = held["repository"].split("/")[1]
    reference = semantics.object_reference(product, raw)
    intent = {"schema": "wharf.release.semantics.retention/v1", "native": observed["native"], "original": observed["original"], "preparation": record, "reference": reference, "reader": observed["reader"], "authentication": False, "qualification": False, "activation": False, "publication": False}
    root.mkdir(parents=True)
    (root / "intent.json").write_bytes(canonical.encode(intent))
    (root / "record.json").write_bytes(raw)
    (root / "inspection.json").write_bytes(canonical.encode(observed["inspection"]))
    source = observed["world"]["source"]
    _, bucket, _ = release.place(release.Release(held["repository"], held["marker"], source["commit"], observed["native"]["commit"]))
    publisher = r2.writer(bucket, "RELEASES")
    for field, name in (("world", "world.json"), ("material", "source.json")):
        require(semantics.retain(publisher, product, observed["files"][name]) == record[field], "retained object reference differs from original native bytes")
    require(reader.identity() == observed["reader"], "shared data reader moved before descriptor retention")
    require(semantics.retain(publisher, product, raw) == reference, "retained world descriptor differs")
    (root / "reference.json").write_bytes(canonical.encode(reference))
    parameters.answer({"digest": reference["sha256"]})
    return intent


def confirm(held):
    root = destination(held, True)
    require(isinstance(held["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", held["digest"]), "world retention confirmation needs its exact descriptor digest")
    names = {"intent.json", "record.json", "inspection.json", "reference.json"}
    require({path.name for path in root.iterdir()} == names and all((root / name).is_file() and not (root / name).is_symlink() for name in names), "world retention receipt file set differs")
    observed = observe(held)
    intent = capture.decode((root / "intent.json").read_bytes())
    record = observed["record"]
    raw = canonical.encode(record)
    reference = semantics.object_reference(held["repository"].split("/")[1], raw)
    require(intent.get("native") == observed["native"] and intent.get("preparation") == record and intent.get("original") == observed["original"], "retained preparation context moved before confirmation")
    require(intent.get("reader") == observed["reader"] and capture.decode((root / "inspection.json").read_bytes()) == observed["inspection"], "shared retained-world reader moved before confirmation")
    require(reference["sha256"] == held["digest"] and capture.decode((root / "reference.json").read_bytes()) == reference and (root / "record.json").read_bytes() == raw, "retained world descriptor bytes differ")
    require(release.fetch(reference["url"]) == raw, "retained world descriptor public bytes differ")
    for field, name in (("world", "world.json"), ("material", "source.json")):
        require(release.fetch(record[field]["url"]) == observed["files"][name], "retained prepared object public bytes differ")
    require(all(intent.get(name) is False for name in ("authentication", "qualification", "activation", "publication")), "retention receipt claims unsupported authority")
    result = {"schema": "wharf.release.semantics.retention-confirmation/v1", "native": observed["native"], "preparation": record, "reference": reference, "authentication": False, "qualification": False, "activation": False, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result
