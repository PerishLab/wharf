import json
import re

from lib.cargo.basis import WIDENED
from lib.content import canonical, implementation
from lib.identity.bind import inventory, storage
from lib.identity.bind.spec import BIND, CARGO
from lib.process.association import binary
from lib.process.association.spec import entry, filename, require, target
from lib.store import plan


def frozen(document, source):
    held = inventory.validate(document["context"].get("binary_declaration"))
    result = inventory.frozen(document["context"].get("native_declaration"), source)
    require(result["manifest"] == held["manifest"] and result["targets"].get(held["target"]) == {name: held[name] for name in ("built", "installed")}, "native manifest differs from the frozen declaration")
    return result


def configuration(basis, selected):
    require(set(basis) == {"entry", "toolchain", "manifest", "members", "lock", "config", "widened"} and basis["widened"] == WIDENED, "native build basis is incomplete")
    held = basis["entry"]
    require(set(held) == {"kind", "packages", "binaries", "target", "runner"} and held["kind"] == "cargo-binary" and held["target"] == selected and held["runner"] == target(selected)["runner"], "native build target or runner differs")
    require(isinstance(held["packages"], list) and held["packages"] and all(isinstance(name, str) and name for name in held["packages"]) and len(set(held["packages"])) == len(held["packages"]), "native build packages are incomplete")
    tools = basis["toolchain"]
    require(set(tools) == {"channel", "profile", "components", "targets"} and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(tools["channel"])) and tools["profile"] == "minimal" and tools["components"] == ["clippy", "rustfmt"] and tools["targets"] == [], "native build toolchain differs")
    require(set(basis["manifest"]) == {"Cargo.toml"} and isinstance(basis["members"], dict) and basis["members"] and set(basis["config"]) <= {".cargo/config.toml"}, "native build source coverage is incomplete")
    require(all(re.fullmatch(r"[0-9a-f]{40}", str(value)) for group in ("manifest", "members", "config") for value in basis[group].values()), "native source object identity is invalid")
    lock = basis["lock"]
    require(isinstance(lock, list) and lock and all(isinstance(row, list) and len(row) == 4 and all(isinstance(value, str) for value in row) and row[0] and row[1] and (not row[3] or re.fullmatch(r"[0-9a-f]{64}", row[3])) for row in lock), "native locked closure is incomplete")
    require(lock == sorted(lock) and len({tuple(row[:3]) for row in lock}) == len(lock), "native locked closure is duplicated or reordered")


def binaries(subject, declared, built, bound):
    source, selected = subject
    names = declared["built"]
    require(names and declared["installed"] and set(declared["installed"]) <= set(names), "native binary inventory is incomplete")
    expected_files = {filename(name, selected) for name in names} | {"receipt.json"}
    require(set(built["files"]) == set(bound["files"]) == expected_files, "native workloads have missing or extra files")
    configuration(built["basis"], selected)
    require(built["basis"]["entry"]["binaries"] == names and bound["basis"] == {"entry": {"kind": "binary-identity", "binary": built["key"]}, "identity": source}, "native workload basis differs from declaration or build")
    receipt = built["receipt"]
    require(receipt.get("action") == "ship.binary" and receipt.get("product") == source["repository"].split("/")[1] and receipt.get("target") == selected and receipt.get("binaries") == names, "native build receipt identity differs")
    require(receipt.get("toolchain") == built["basis"]["toolchain"]["channel"] and receipt.get("packages") == built["basis"]["entry"]["packages"] and isinstance(receipt.get("rustc"), str) and receipt["rustc"], "native build receipt configuration differs")
    require(receipt.get("files") == {name: entry(body) for name, body in built["files"].items() if name != "receipt.json"}, "native build receipt bytes differ")
    result = {}
    for name in names:
        body = built["files"][filename(name, selected)]
        actual = bound["files"][filename(name, selected)]
        normalized, facts = binary.inspect(actual, (selected, source, built["key"]), name)
        require(binary.normalized(body, name, selected) == normalized, "native binding changed complete effective executable content")
        binding = {"product": source["repository"].split("/")[1], "marker": source["marker"], "digest": canonical.digest(source), "commit": source["commit"], "workload": built["key"]}
        require(binary.transformed(body, binding, name) == actual, "native binding or signature is not the exact permitted transformation")
        result[name] = facts
    receipt = bound["receipt"]
    require(receipt.get("action") == "ship.identity" and receipt.get("binding") == binding, "native binding receipt identity differs")
    expected = [{"file": filename(name, selected), "origin": result[name]["origin"], "signature": target(selected)["signature"], **entry(bound["files"][filename(name, selected)])} for name in sorted(names)]
    require(receipt.get("binaries") == expected, "native binding receipt inventory or signature differs")
    return result


def verify(bucket, source, document, declared):
    model, selected = declared
    specification = target(selected)
    context = document["context"]
    require(all(context.get(name) == value for name, value in source.items()) and plan.combination(context["snapshot"])["source"] == source, "native plan source differs")
    names = ("binary-" + specification["unit"], "bind-" + specification["unit"])
    require(all(name in document["entries"] for name in names), "native plan has no complete target build chain")
    built = storage.read(bucket, document["entries"][names[0]]["key"], implementation.resourced(*CARGO))
    bound = storage.read(bucket, document["entries"][names[1]]["key"], implementation.resourced(*BIND))
    require(built["basis"]["toolchain"]["channel"] == context["snapshot"]["domain"]["rust.version"], "native toolchain differs from publication resolution")
    for loaded, name in zip((built, bound), names):
        require(loaded["record"].get("context", {}).get("repository") == source["repository"], "native workload producer belongs to another product")
        storage.producer(bucket, loaded, name)
    facts = binaries((source, selected), model["targets"][selected], built, bound)
    return {"target": selected, "declaration": model, "build": built, "binding": bound, "binaries": facts}
