import json
import tempfile
from pathlib import Path

from lib.content import executables
from lib.identity.bind.spec import TARGET, require, sha
from lib.media import release


def capture(source):
    path = Path(source) / "plumb.toml"
    require(path.is_file() and not path.is_symlink(), "binary declaration needs a regular plumb.toml")
    targets = release.targets(source)
    listed = executables.declared(source, targets)
    return {"manifest": sha(path.read_bytes()), "target": TARGET, "built": executables.built(listed, TARGET), "installed": executables.installed(listed, TARGET)}


def validate(value):
    require(isinstance(value, dict) and set(value) == {"manifest", "target", "built", "installed"}, "frozen binary declaration fields differ")
    from lib.identity.bind.spec import digest
    require(digest(value["manifest"]) and value["target"] == TARGET, "frozen binary declaration identity differs")
    for name in ("built", "installed"):
        entries = value[name]
        require(isinstance(entries, list) and entries and all(isinstance(item, str) and executables.NAME.fullmatch(item) for item in entries) and len(set(entries)) == len(entries), "frozen binary declaration inventory is incomplete")
    require(set(value["installed"]) <= set(value["built"]), "installed binary is outside the frozen build")
    return value


def resolve(public, document):
    held = document["context"].get("binary_declaration")
    if held is not None:
        return validate(held)
    trial = document["context"].get("trial")
    require(isinstance(trial, dict) and trial == public["distribution"].get("trial") and trial.get("source") == public["source"], "historical publication has no frozen binary declaration")
    source = public["source"]
    raw = release.fetch(f"https://raw.githubusercontent.com/{source['repository']}/{source['commit']}/plumb.toml")
    require(sha(raw) == trial.get("manifest"), "historical declaration differs from its published manifest digest")
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "plumb.toml").write_bytes(raw)
        held = capture(directory)
    return validate(held)
