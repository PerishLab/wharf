import ast
import tempfile
from pathlib import Path

from lib.content import canonical, implementation, marker, resources
from lib.media import release
from lib.identity.bind.spec import digest
from lib.process.association.spec import document, entry, fetch, require, sha


def rendered(source, model):
    name = source["repository"].split("/")[1]
    installed = {target: held["installed"] for target, held in model["targets"].items()}
    windows = release.windowed(name, installed)
    if windows is not None:
        windows.update(windows_scope=model["windows"], windows_name=name[:1].upper() + name[1:])
    result = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        release.filled(release.named(name, source["marker"], marker.channel(source["marker"]), installed), windows, root)
        result["pinned"] = {platform: (root / file).read_bytes() for platform, (file, _) in release.MANAGERS.items() if (root / file).is_file()}
        release.filled(release.named(name, "", "stable", installed), windows, root)
        result["normalized"] = {platform: (root / file).read_bytes() for platform, (file, _) in release.MANAGERS.items() if (root / file).is_file()}
    return result


def verify(public, model):
    source = public["source"]
    produced = rendered(source, model)
    generator = provenance(public)
    held = public["seal"].get("managers")
    require(isinstance(held, dict) and set(held) == set(produced["pinned"]), "manager inventory differs from the complete declaration")
    _, _, authority = release.place(release.Release(source["repository"], source["marker"], source["commit"], ""))
    for platform, body in produced["pinned"].items():
        file, mime = release.MANAGERS[platform]
        reference = release.remote(authority, "v1/objects/sha256/" + sha(body) + "/" + file, body, mime)
        require(held[platform] == reference and fetch(reference["url"], 65536) == body, "published manager differs from its exact template transformation")
    return {"implementation": renderer(), "generator": generator, "pinned": held, "normalized": {name: entry(body) for name, body in produced["normalized"].items()}, "canonical": rooted(public, produced["normalized"])}


def associated(left, right):
    require(left["implementation"] == right["implementation"] and left["normalized"] == right["normalized"], "manager effective content or implementation changed")


def rooted(public, bodies):
    source = public["source"]
    if marker.channel(source["marker"]) != "stable":
        return {"state": "not-published", "managers": {}}
    _, _, authority = release.place(release.Release(source["repository"], source["marker"], source["commit"], ""))
    url = authority + "/v1/channels/stable.json"
    raw = fetch(url)
    pointer = document(raw)
    require(all(pointer.get(name) == expected for name, expected in (("product", source["repository"].split("/")[1]), ("channel", "stable"), ("releaseVersion", source["marker"]), ("commit", source["commit"]))) and pointer.get("seal") == public["seal_ref"], "canonical managers do not belong to this stable pointer")
    references = {}
    for platform, body in bodies.items():
        file, mime = release.MANAGERS[platform]
        references[platform] = release.remote(authority, file, body, mime)
        require(fetch(authority + "/" + file, 65536) == body, "canonical manager bytes differ from its exact template")
    require(pointer.get("managers") == references and fetch(url) == raw, "canonical manager pointer moved or differs")
    return {"state": "associated", "pointer": sha(raw), "managers": references}


def anticipated(source, model):
    produced = rendered(source, model)
    _, _, authority = release.place(release.Release(source["repository"], source["marker"], source["commit"], ""))
    pinned = {name: release.remote(authority, "v1/objects/sha256/" + sha(body) + "/" + release.MANAGERS[name][0], body, release.MANAGERS[name][1]) for name, body in produced["pinned"].items()}
    return {"implementation": renderer(), "generator": None, "pinned": pinned, "normalized": {name: entry(body) for name, body in produced["normalized"].items()}, "canonical": {"state": "expected", "managers": {}}}


def renderer():
    names = {"archived", "platforms", "windowed", "named", "written", "filled"}
    tree = ast.parse(implementation.source("lib.media.release").read_text())
    functions = {node.name: ast.dump(node, include_attributes=False) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names}
    require(set(functions) == names, "manager renderer function inventory differs")
    files = {name: sha(resources.read_bytes(name)) for name in ("releases.json", *release.TEMPLATES.values())}
    return canonical.digest({"schema": "wharf.manager.template/v1", "functions": functions, "constants": {"managers": release.MANAGERS, "templates": release.TEMPLATES, "probe": release.PROBE}, "fill": implementation.digest("lib.content.fill"), "resources": files})


def provenance(public):
    held = public["seal"].get("generator")
    source = public["publisher"]["wharf"]
    require(isinstance(held, dict) and set(held) == {"version", "template", "origin"} and held["version"] == "wharf " + source[:12] and digest(held["template"]) and held["origin"] == {"kind": "source-built", "repository": "https://github.com/PerishLab/wharf", "commit": source}, "manager generator provenance differs from its native publisher")
    return held
