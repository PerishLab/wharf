import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import index, manifest, registry, toolchain
from lib.process import run
from lib.refusal import Conflict, Refusal
from lib.store import r2

ROLE = "CARGO"
INDEX = {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"}
CRATE = {"Content-Type": "application/gzip", "Cache-Control": "public, max-age=31536000, immutable"}


@dataclass(frozen=True)
class Tools:
    run: object = run
    reader: object = registry.fetch
    sleep: object = time.sleep
    store: object = r2.writer


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


def packaged(source, held, version, tools):
    channel = toolchain.declared(source)["channel"]
    with tempfile.TemporaryDirectory() as directory:
        argv = ["cargo", "package", "--locked", "--no-verify", "--allow-dirty", "--registry", held["registry"], "--package", held["package"], "--target-dir", directory]
        tools.run(argv, source, dict(os.environ, RUSTUP_TOOLCHAIN=channel))
        return (Path(directory) / "package" / f"{held['package']}-{version}.crate").read_bytes()


def stored(bucket, key, blob):
    try:
        bucket.create(key, blob, CRATE)
    except Conflict:
        if bucket.get(key) != blob:
            raise Refusal(f"{key} already holds different bytes")


def appended(bucket, key, line, tools):
    for _ in range(5):
        etag = bucket.head(key)
        lines = [held for held in (bucket.get(key).decode() if etag else "").splitlines() if held.strip()]
        if any(json.loads(held)["vers"] == line["vers"] for held in lines):
            raise Refusal(f"{key} already lists {line['vers']}")
        body = "".join(held + "\n" for held in lines + [json.dumps(line, separators=(",", ":"))])
        condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
        try:
            bucket.conditional(r2.Operation("PUT", key, body.encode(), dict(INDEX, **condition)))
            return
        except Conflict:
            tools.sleep(1)
    raise Refusal(f"{key} kept changing while {line['name']} {line['vers']} was appended")


def placed(source, held, version, tools):
    location = registry.declared(source, held["registry"])
    blob = packaged(source, held, version, tools)
    line = index.entry(blob, held["package"], version, location)
    bucket = tools.store(registry.bucket(held["registry"]), ROLE)
    stored(bucket, registry.download(location, line, tools.reader), blob)
    appended(bucket, registry.entry(held["package"]), line, tools)
    visible(location, held["package"], version, tools)


def publish(source, version, tools=Tools()):
    source = Path(source)
    results = []
    for held in pending(source, version, tools.reader):
        if held["published"]:
            results.append({"package": held["package"], "state": "already-published"})
            continue
        placed(source, held, version, tools)
        results.append({"package": held["package"], "state": "published"})
    return {"version": version, "packages": results}
