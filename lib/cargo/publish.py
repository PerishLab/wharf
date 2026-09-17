import os
import time
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import manifest, registry, toolchain
from lib.process import run
from lib.refusal import Refusal

TOKEN = "WHARF_CARGO_TOKEN"


@dataclass(frozen=True)
class Tools:
    run: object = run
    reader: object = registry.fetch
    sleep: object = time.sleep


def publishable(source):
    source = Path(source)
    workspace = manifest.members(source, manifest.read(source, "Cargo.toml"))
    found = {}
    for package, directory in workspace.items():
        target = manifest.read(source, f"{directory}/Cargo.toml")["package"].get("publish", True)
        if target is False:
            continue
        if not isinstance(target, list) or len(target) != 1:
            raise Refusal(f"{package} must publish to exactly one named registry or declare publish = false")
        found[package] = target[0]
    needs = {package: {member for member in manifest.closure(source, workspace, package) if member in found and member != package} for package in found}
    ordered = []
    while len(ordered) < len(found):
        ready = sorted(package for package in found if package not in ordered and needs[package] <= set(ordered))
        if not ready:
            raise Refusal("publishable packages depend on each other in a cycle")
        ordered.append(ready[0])
    return [(package, found[package]) for package in ordered]


def pending(source, version, reader=registry.fetch):
    held = []
    for package, name in publishable(source):
        location = registry.declared(source, name)
        held.append({"package": package, "registry": name, "published": registry.published(location, package, version, reader)})
    return held


def visible(location, package, version, tools):
    for _ in range(40):
        if registry.published(location, package, version, tools.reader):
            return
        tools.sleep(3)
    raise Refusal(f"{package} {version} did not appear in the cargo index")


def publish(source, version, tools=Tools()):
    source = Path(source)
    token = os.environ.get(TOKEN, "")
    if not token:
        raise Refusal(f"{TOKEN} is required to publish")
    channel = toolchain.declared(source)["channel"]
    results = []
    for held in pending(source, version, tools.reader):
        if held["published"]:
            results.append({"package": held["package"], "state": "already-published"})
            continue
        env = dict(os.environ, RUSTUP_TOOLCHAIN=channel)
        env[f"CARGO_REGISTRIES_{held['registry'].upper().replace('-', '_')}_TOKEN"] = f"Bearer {token}"
        argv = ["cargo", "publish", "--locked", "--no-verify", "--allow-dirty", "--registry", held["registry"], "--package", held["package"]]
        tools.run(argv, source, env)
        visible(registry.declared(source, held["registry"]), held["package"], version, tools)
        results.append({"package": held["package"], "state": "published"})
    return {"version": version, "packages": results}
