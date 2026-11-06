import json
import tempfile
from pathlib import Path

from lib.content import declaration, executables
from lib.identity.bind import control
from lib.identity.bind.spec import TARGET, digest, require, sha
from lib.media import release
from lib.refusal import Refusal


def capture(source):
    path = Path(source) / "plumb.toml"
    require(path.is_file() and not path.is_symlink(), "binary declaration needs a regular plumb.toml")
    require(path.stat().st_size <= 1048576, "binary declaration is outside bounds")
    return linux(model(path.read_bytes()))


def model(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= 1048576, "native manifest is outside bounds")
    try:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "plumb.toml").write_bytes(raw)
            targets = release.targets(directory)
            listed = executables.declared(directory, targets)
            scope = declaration.windows(directory)
    except (OSError, ValueError, TypeError):
        raise Refusal("binary declaration cannot be parsed") from None
    return complete({"manifest": sha(raw), "targets": {name: {"built": executables.built(listed, name), "installed": executables.installed(listed, name)} for name in targets}, "windows": scope})


def complete(value):
    require(isinstance(value, dict) and set(value) == {"manifest", "targets", "windows"}, "complete binary declaration fields differ")
    require(digest(value["manifest"]) and value["windows"] in ("user", "machine"), "complete binary declaration identity or Windows scope differs")
    targets = value["targets"]
    require(isinstance(targets, dict) and set(targets) <= set(release.LAYOUT["targets"]), "complete binary declaration targets differ")
    for held in targets.values():
        require(isinstance(held, dict) and set(held) == {"built", "installed"}, "complete binary target fields differ")
        for entries in held.values():
            require(isinstance(entries, list) and len(entries) <= 256 and all(isinstance(name, str) and len(name) <= 256 and executables.NAME.fullmatch(name) for name in entries) and len(entries) == len(set(entries)), "complete binary target inventory differs")
        require(set(held["installed"]) <= set(held["built"]), "complete installed inventory is outside the build")
    return value


def linux(value):
    held = complete(value)
    return {"manifest": held["manifest"], "target": TARGET, **held["targets"].get(TARGET, {"built": [], "installed": []})}


def native(source, identity):
    control.source(identity)
    path = Path(source) / "plumb.toml"
    require(path.is_file() and not path.is_symlink(), "native declaration needs a regular plumb.toml")
    require(path.stat().st_size <= 1048576, "native declaration is outside bounds")
    return {"schema": "wharf.binary.declaration/v1", "source": dict(identity), "model": model(path.read_bytes())}


def frozen(value, source):
    control.source(source)
    require(isinstance(value, dict) and set(value) == {"schema", "source", "model"} and value["schema"] == "wharf.binary.declaration/v1", "frozen native declaration fields or schema differ")
    require(value["source"] == source, "frozen native declaration belongs to another source")
    return complete(value["model"])


def validate(value):
    require(isinstance(value, dict) and set(value) == {"manifest", "target", "built", "installed"}, "frozen binary declaration fields differ")
    require(digest(value["manifest"]) and value["target"] == TARGET, "frozen binary declaration identity differs")
    for name in ("built", "installed"):
        entries = value[name]
        require(isinstance(entries, list) and entries and all(isinstance(item, str) and executables.NAME.fullmatch(item) for item in entries) and len(set(entries)) == len(entries), "frozen binary declaration inventory is incomplete")
    require(set(value["installed"]) <= set(value["built"]), "installed binary is outside the frozen build")
    return value


def planned(context, source):
    held = context.get("binary_declaration")
    if held is not None:
        return validate(held)
    trial = context.get("trial")
    require(isinstance(trial, dict) and trial.get("source") == source, "historical plan has no frozen binary declaration")
    raw = release.fetch(f"https://raw.githubusercontent.com/{source['repository']}/{source['commit']}/plumb.toml")
    require(sha(raw) == trial.get("manifest"), "historical declaration differs from its published manifest digest")
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "plumb.toml").write_bytes(raw)
        held = capture(directory)
    return validate(held)


def resolve(public, document):
    context = document["context"]
    if context.get("binary_declaration") is None:
        trial = context.get("trial")
        require(isinstance(trial, dict) and trial == public["distribution"].get("trial") and trial.get("source") == public["source"], "historical publication has no frozen binary declaration")
    return planned(context, public["source"])
