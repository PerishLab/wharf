import re
from pathlib import Path

from lib import parameters
from lib.cargo import toolchain
from lib.content import canonical
from lib.identity.bind import control
from lib.identity.bind.spec import entry, require
from lib.identity.guard import snapshot
from lib.process.confirmation import materials, semantics
from lib.process.promotion import capture, preflight, reader, subject

SCHEMA = "wharf.release.semantics.preparation/v1"
SELECTION = "wharf.release.semantics.selection/v1"


def destination(held, existing):
    path = Path(held["artifact"])
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), "semantic preparation needs a canonical owned artifact path")
    require(not path.is_relative_to(Path.cwd().resolve()) and not path.is_relative_to(Path(held.get("source", ".")).resolve()), "semantic preparation artifacts must be outside source")
    require(path.is_dir() if existing else not path.exists(), "semantic preparation artifact presence differs")
    return path


def identity(held, requested):
    repository = held["repository"]
    prefix = f"repos/{repository}"
    actual = subject.api(prefix)
    require(actual.get("full_name") == repository and actual.get("visibility") == "public" and actual.get("archived") is False and actual.get("disabled") is False, "semantic preparation repository is unavailable")
    require(actual.get("owner", {}).get("id") == 330236468 and actual["owner"].get("node_id") == "O_kgDOE68CNA" and actual.get("default_branch") == "main", "semantic preparation repository authority differs")
    branch = subject.api(prefix + "/branches/main")
    require(branch.get("name") == "main" and branch.get("protected") is True, "semantic preparation needs protected product main")
    head = branch.get("commit", {}).get("sha")
    require(isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head), "semantic product main identity is invalid")
    compared = subject.api(prefix + f"/compare/{requested['commit']}...{head}")
    require(compared.get("base_commit", {}).get("sha") == requested["commit"] and compared.get("merge_base_commit", {}).get("sha") == requested["commit"] and compared.get("status") in ("ahead", "identical"), "semantic preparation source is outside protected product main history")
    commit = subject.api(prefix + "/git/commits/" + requested["commit"])
    require(commit.get("sha") == requested["commit"], "semantic source commit differs")
    return control.source({"repository": repository, "marker": held["marker"], "commit": requested["commit"], "tree": commit.get("tree", {}).get("sha")})


def source(held):
    root = destination(held, False)
    native = capture.active(held, "semantics-world")
    requested = preflight.semantics(preflight.event()["semantics"])
    point = identity(held, requested)
    authority = reader.identity()
    collection = semantics.collected(point, requested["scenes"])
    require(reader.identity() == authority, "formal semantic reader moved during source selection")
    selected = {"schema": SELECTION, "native": native, "source": point, "scenes": requested["scenes"], "reader": authority}
    root.mkdir(parents=True)
    (root / "selection.json").write_bytes(canonical.encode(selected))
    (root / "collection.json").write_bytes(canonical.encode(collection))
    parameters.answer({"commit": point["commit"], "tree": point["tree"]})
    return selected


def selected(held, root):
    native = capture.active(held, "semantics-world")
    requested = preflight.semantics(preflight.event()["semantics"])
    value = capture.decode((root / "selection.json").read_bytes())
    require(isinstance(value, dict) and set(value) == {"schema", "native", "source", "scenes", "reader"} and value["schema"] == SELECTION and value["native"] == native, "semantic selection has another schema or native preparation")
    require(isinstance(value["source"], dict) and set(value["source"]) == {"repository", "marker", "commit", "tree"}, "semantic source selection fields differ")
    require(all(value["source"][name] == held[name] for name in ("repository", "marker")) and value["source"]["commit"] == requested["commit"] and value["scenes"] == requested["scenes"], "semantic selection differs from actual request")
    return value


def produce(held):
    root = destination(held, True)
    selection = selected(held, root)
    collection = (root / "collection.json").read_bytes()
    observed = capture.decode(collection)
    require(isinstance(observed, dict) and observed.get("state") == "observed" and observed.get("snapshot", {}).get("source") == {name: selection["source"][name] for name in ("repository", "commit", "tree")}, "selected collection does not bind the original source")
    require(all(not (root / name).exists() for name in ("world.json", "source.json", "inspection.json", "preparation.json")), "preserve an existing semantic world preparation instead of rebuilding it")
    require(reader.identity() == selection["reader"], "formal semantic reader moved before world preparation")
    request = snapshot.Request(Path(held["source"]), selection["source"], toolchain.versions())
    with snapshot.prepared(request) as (checkout, world, unchanged):
        require(world["source"] == selection["source"], "prepared world names another selected source")
        require(world.get("controls", {}).get("authority") == selection["reader"], "prospective world did not use the selected formal control")
        payload = materials.capture(checkout, selection["source"], world)
        unchanged()
    raw = canonical.encode(world)
    authority, inspected = capture.inspect(world, raw)
    require(authority == selection["reader"], "formal semantic reader moved during world preparation")
    packed = canonical.encode(payload)
    require((root / "collection.json").read_bytes() == collection, "selected collection moved during product execution")
    prepared = {"schema": SCHEMA, "native": selection["native"], "source": selection["source"], "scenes": selection["scenes"], "reader": authority, "world": {"name": "world.json", **entry(raw)}, "material": {"name": "source.json", **entry(packed)}, "collection": {"name": "collection.json", **entry(collection)}, "adoption": False, "qualification": False, "activation": False, "publication": False}
    for name, body in {"world.json": raw, "source.json": packed, "inspection.json": canonical.encode(inspected), "preparation.json": canonical.encode(prepared)}.items():
        (root / name).write_bytes(body)
    parameters.answer({"digest": prepared["world"]["sha256"], "payload": prepared["material"]["sha256"]})
    return prepared


def confirm(held):
    root = destination(held, True)
    selection = selected(held, root)
    files = {"selection.json", "collection.json", "world.json", "source.json", "inspection.json", "preparation.json"}
    require({path.name for path in root.iterdir()} == files and all((root / name).is_file() and not (root / name).is_symlink() for name in files), "semantic preparation file set differs")
    expected = capture.decode(held["observation"].encode())
    require(isinstance(expected, dict) and set(expected) == {"world", "source"} and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in expected.values()), "semantic confirmation needs exact world and source digests")
    prepared = capture.decode((root / "preparation.json").read_bytes())
    require(isinstance(prepared, dict) and prepared.get("schema") == SCHEMA and all(prepared.get(name) == selection[name] for name in ("native", "source", "scenes", "reader")), "prepared semantic context moved before confirmation")
    require(all(prepared.get(name) is False for name in ("adoption", "qualification", "activation", "publication")), "prepared data exceeds its observation role")
    for key, field in (("world", "world"), ("source", "material"), (None, "collection")):
        reference = prepared[field]
        require(reference.get("name") == {"world": "world.json", "material": "source.json", "collection": "collection.json"}[field], "semantic preparation file selection differs")
        raw = (root / reference["name"]).read_bytes()
        require(entry(raw) == {name: reference[name] for name in ("sha256", "size")}, "semantic preparation file bytes differ")
        require(key is None or reference["sha256"] == expected[key], "semantic confirmation digest differs from actual prepared bytes")
    world = capture.decode((root / "world.json").read_bytes())
    payload = capture.decode((root / "source.json").read_bytes())
    require(payload.get("schema") == "wharf.release.source/v1" and payload.get("source") == world.get("source") == selection["source"] and all(payload.get(name) == world.get(name) for name in ("head", "tree")), "effective source payload names another actual world")
    authority, inspected = capture.inspect(world, canonical.encode(world))
    require(authority == selection["reader"] and inspected == capture.decode((root / "inspection.json").read_bytes()), "formal prepared-world inspection moved before confirmation")
    result = {"schema": "wharf.release.semantics.confirmation/v1", "native": selection["native"], "world": prepared["world"], "material": prepared["material"], "adoption": False, "qualification": False, "activation": False, "publication": False}
    (root / "confirmation.json").write_bytes(canonical.encode(result))
    return result
