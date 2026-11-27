import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import basis, publish, suite, toolchain
from lib.content import declaration, resources
from lib.refusal import Refusal
from lib.store.plan import decided

BUILD = resources.read_json("build/config.json")
PRIMARY = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])
KNOWN = {target["target"] for target in BUILD["targets"]}
DEFAULTS = ("x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc")
HOSTS = {"aarch64-unknown-linux-musl": "aarch64-unknown-linux-gnu"}
MODULES = (["lib.cargo.basis", "lib.cargo.suite.check"], ["build/config.json"])


@dataclass(frozen=True)
class Check:
    source: Path
    output: Path
    target: str
    packages: tuple


def packages(source):
    return tuple(package for package, _ in publish.publishable(source))


def targeted(target):
    for known in BUILD["targets"]:
        if target in (known["target"], known["name"]):
            return known
    raise Refusal(f"target {target} is not one this repository builds")


def validated(declared):
    if not isinstance(declared, list) or not declared or not all(isinstance(item, str) for item in declared):
        raise Refusal("[release.cargo].targets must list at least one target")
    if len(set(declared)) != len(declared):
        raise Refusal("[release.cargo].targets must not duplicate targets")
    unknown = [item for item in declared if item not in KNOWN]
    if unknown:
        raise Refusal(f"[release.cargo].targets declares unknown targets: {', '.join(unknown)}")
    if PRIMARY["target"] not in declared:
        raise Refusal(f"[release.cargo].targets must include {PRIMARY['target']}")
    return declared


def targets(source):
    if not packages(source):
        return []
    attachment = declaration.release(source).get("cargo") or {}
    selected = validated(attachment["targets"]) if "targets" in attachment else DEFAULTS
    return [item for item in BUILD["targets"] if item["target"] in selected]


def plan(source, covered, bucket, entries):
    names = packages(source)
    if not names:
        return
    for target in targets(source):
        if f"suite-{target['name']}" in entries or (target["name"] == BUILD["primary"] and covered):
            continue
        held_basis = basis.cargo_check(source, names, target["target"], target["runner"])
        entries[f"cargo-check-{target['name']}"] = decided(bucket, held_basis, MODULES, entries)


def execute(request, tools, channel):
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as target:
        env = suite.isolated(channel, home, target)
        rustc = tools.run(["rustc", "-vV"], request.source, env).strip()
        hosts = [line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: ")]
        expected = HOSTS.get(request.target, request.target)
        if hosts != [expected]:
            raise Refusal(f"cargo check needs native host {expected}, rustc reports {hosts}")
        argv = ["cargo", "check", "--locked", "--target", request.target, "--lib", "--bins"] + [
            part for package in request.packages for part in ("-p", package)
        ]
        code, log = tools.attempt(argv, request.source, env)
    return code, log, rustc


def check(request, tools=suite.Tools()):
    request = Check(Path(request.source).resolve(), Path(request.output), request.target, tuple(request.packages))
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    if not request.packages:
        raise Refusal("cargo check names at least one publishable package")
    declared = tools.toolchain()
    channel = declared["channel"]
    tools.run(toolchain.install(declared, [request.target]), request.source)
    code, _, rustc = execute(request, tools, channel)
    if code != 0:
        raise Refusal(f"cargo check exited {code} on {request.target} for {', '.join(request.packages)}")
    request.output.mkdir(parents=True)
    receipt = {
        "action": "ship.cargo.check",
        "toolchain": channel,
        "target": request.target,
        "packages": list(request.packages),
        "rustc": rustc,
    }
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
