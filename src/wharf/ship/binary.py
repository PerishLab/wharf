"""Atomic action: build one Cargo binary for one target triple.

The action knows nothing about markers, other artifacts or the order they run in.
It takes a checked-out source tree, refuses early when that tree cannot yield the
requested binary, and leaves exactly one executable plus one receipt behind.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship import cargo

TRIPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_]+){2,3}$")


def run(argv, cwd, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def locate(source, name, runner=run):
    if not (source / "Cargo.lock").is_file():
        raise Refusal(f"{source} has no Cargo.lock; a locked build is required")
    metadata = json.loads(runner(["cargo", "metadata", "--locked", "--no-deps", "--format-version", "1"], source))
    owners = [
        package["name"]
        for package in metadata["packages"]
        for target in package["targets"]
        if target["name"] == name and "bin" in target["kind"]
    ]
    if len(owners) != 1:
        raise Refusal(f"binary {name!r} must be declared by exactly one package, found {owners}")
    return owners[0]


def build(source, name, triple, output, runner=run, declared=cargo.toolchain):
    source = Path(source).resolve()
    output = Path(output)
    if not TRIPLE.match(triple):
        raise Refusal(f"target triple {triple!r} is malformed")
    if output.exists():
        raise Refusal(f"output {output} already exists")
    toolchain = declared(source)["channel"]

    runner(["rustup", "toolchain", "install", toolchain, "--profile", "minimal", "--target", triple], source)
    env = dict(os.environ, RUSTUP_TOOLCHAIN=toolchain)
    package = locate(source, name, lambda argv, cwd: runner(argv, cwd, env))
    rustc = runner(["rustc", "--version"], source, env).strip()

    with tempfile.TemporaryDirectory() as target:
        env["CARGO_TARGET_DIR"] = target
        runner(
            ["cargo", "build", "--locked", "--release", "--package", package, "--bin", name, "--target", triple],
            source,
            env,
        )
        suffix = ".exe" if "windows" in triple else ""
        built = Path(target) / triple / "release" / f"{name}{suffix}"
        if not built.is_file():
            raise Refusal(f"cargo reported success but {built} is missing")
        output.mkdir(parents=True)
        artifact = output / f"{name}-{triple}{suffix}"
        shutil.copy2(built, artifact)

    body = artifact.read_bytes()
    receipt = {
        "action": "ship.binary",
        "binary": name,
        "package": package,
        "target": triple,
        "toolchain": toolchain,
        "rustc": rustc,
        "file": artifact.name,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def smoke(directory, name, triple, output, runner=run):
    """Run a fetched binary's version and help surfaces; both must exit zero."""
    directory = Path(directory)
    output = Path(output)
    suffix = ".exe" if "windows" in triple else ""
    artifact = directory / f"{name}-{triple}{suffix}"
    if not artifact.is_file():
        raise Refusal(f"{artifact} is missing")
    if output.exists():
        raise Refusal(f"output {output} already exists")
    artifact.chmod(0o755)
    results = {}
    for surface in ("--version", "--help"):
        try:
            results[surface] = runner([str(artifact.resolve()), surface], directory).strip()[:2000]
        except subprocess.CalledProcessError as failure:
            raise Refusal(f"{artifact.name} {surface} exited {failure.returncode}: {(failure.stderr or failure.stdout or '').strip()[:500]}")
    output.mkdir(parents=True)
    receipt = {"action": "ship.binary.smoke", "file": artifact.name, "surfaces": results}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
