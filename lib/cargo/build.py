import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import toolchain
from lib.process import run
from lib.refusal import Refusal

TRIPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_]+){2,3}$")


@dataclass(frozen=True)
class Build:
    source: Path
    name: str
    target: str
    output: Path

    @property
    def suffix(self):
        return ".exe" if "windows" in self.target else ""

    @property
    def artifact(self):
        return f"{self.name}-{self.target}{self.suffix}"


@dataclass(frozen=True)
class Tools:
    run: object = run
    toolchain: object = toolchain.declared


def locate(source, name, execute):
    if not (source / "Cargo.lock").is_file():
        raise Refusal(f"{source} has no Cargo.lock; a locked build is required")
    metadata = json.loads(execute(["cargo", "metadata", "--locked", "--no-deps", "--format-version", "1"]))
    owners = [
        package["name"]
        for package in metadata["packages"]
        for target in package["targets"]
        if target["name"] == name and "bin" in target["kind"]
    ]
    if len(owners) != 1:
        raise Refusal(f"binary {name!r} must be declared by exactly one package, found {owners}")
    return owners[0]


def environment(request, channel):
    prefix = request.name.upper().replace("-", "_")
    env = dict(os.environ, RUSTUP_TOOLCHAIN=channel)
    env[f"{prefix}_BUILD_TARGET"] = request.target
    env[f"{prefix}_BUILD_CHANNEL"] = "unbound"
    return env


def produce(request, tools, env):
    with tempfile.TemporaryDirectory() as target:
        env["CARGO_TARGET_DIR"] = target
        package = locate(request.source, request.name, lambda argv: tools.run(argv, request.source, env))
        argv = ["cargo", "build", "--locked", "--release", "--package", package, "--bin", request.name, "--target", request.target]
        tools.run(argv, request.source, env)
        built = Path(target) / request.target / "release" / f"{request.name}{request.suffix}"
        if not built.is_file():
            raise Refusal(f"cargo reported success but {built} is missing")
        request.output.mkdir(parents=True)
        shutil.copy2(built, request.output / request.artifact)
        return package


def build(request, tools=Tools()):
    request = Build(Path(request.source).resolve(), request.name, request.target, Path(request.output))
    if not TRIPLE.match(request.target):
        raise Refusal(f"target triple {request.target!r} is malformed")
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    channel = tools.toolchain(request.source)["channel"]
    tools.run(["rustup", "toolchain", "install", channel, "--profile", "minimal", "--target", request.target], request.source)
    env = environment(request, channel)
    package = produce(request, tools, env)
    rustc = tools.run(["rustc", "--version"], request.source, env).strip()
    body = (request.output / request.artifact).read_bytes()
    receipt = {
        "action": "ship.binary",
        "binary": request.name,
        "package": package,
        "target": request.target,
        "toolchain": channel,
        "rustc": rustc,
        "file": request.artifact,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
