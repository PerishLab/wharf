import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import manifest, toolchain
from lib.process import run, stream
from lib.refusal import Refusal

TRIPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_]+){2,3}$")
HASHED = re.compile(r"-[0-9a-f]{8,}$")
PROBED = ".rustc_info.json"
NATIVE_PERL = Path("C:/Strawberry/perl/bin/perl.exe")


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
    stream: object = stream
    toolchain: object = toolchain.declared


def locked(source):
    if not (source / "Cargo.lock").is_file():
        raise Refusal(f"{source} has no Cargo.lock; a locked build is required")


def owner(name, metadata):
    owners = [
        package["name"]
        for package in json.loads(metadata)["packages"]
        for target in package["targets"]
        if target["name"] == name and "bin" in target["kind"]
    ]
    if len(owners) != 1:
        raise Refusal(f"binary {name!r} must be declared by exactly one package, found {owners}")
    return owners[0]


def timed(runner, argv, cwd, env=None):
    started = time.monotonic()
    output = runner(argv, cwd, env)
    return output, round(time.monotonic() - started, 1)


def environment(request, channel):
    prefix = request.name.upper().replace("-", "_")
    env = dict(os.environ, RUSTUP_TOOLCHAIN=channel)
    env[f"{prefix}_BUILD_TARGET"] = request.target
    env[f"{prefix}_BUILD_CHANNEL"] = "unbound"
    if request.target.endswith("-windows-msvc") and NATIVE_PERL.is_file():
        env.setdefault("OPENSSL_SRC_PERL", str(NATIVE_PERL))
    return env


def workspace(source):
    return Path(source).resolve() / "target"


def crates(source):
    return set(manifest.members(source, manifest.read(source, "Cargo.toml")))


def stem(part):
    base = part[3:] if part.startswith("lib") else part
    return HASHED.sub("", base.split(".")[0])


def owned(relative, named):
    return any(stem(part) in named for part in Path(relative).parts)


def held(directory, named):
    found = (path for path in Path(directory).rglob("*") if path.is_file() and not path.is_symlink())
    names = sorted(path.relative_to(directory).as_posix() for path in found)
    return [name for name in names if name != PROBED and not owned(name, named)]


def archive(source, output):
    directory = workspace(source)
    named = {name.replace("-", "_") for name in crates(source)} | crates(source)
    with open(output, "wb") as body, gzip.GzipFile(filename="", mode="wb", fileobj=body, mtime=0, compresslevel=1) as packed:
        with tarfile.open(fileobj=packed, mode="w", format=tarfile.GNU_FORMAT) as archived:
            for name in held(directory, named):
                path = directory / name
                entry = tarfile.TarInfo(name)
                entry.size, entry.mtime, entry.uid, entry.gid, entry.uname, entry.gname = path.stat().st_size, 0, 0, 0, "", ""
                entry.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
                with path.open("rb") as reading:
                    archived.addfile(entry, reading)
    return output


def restore(source, packed):
    directory = workspace(source)
    named = {name.replace("-", "_") for name in crates(source)} | crates(source)
    with gzip.open(packed, "rb") as body, tarfile.open(fileobj=body, mode="r|") as archived:
        archived.extractall(directory, filter="data")
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file() and owned(path.relative_to(directory).as_posix(), named):
            path.unlink()
    stamped = time.time()
    for path in directory.rglob("*"):
        os.utime(path, (stamped, stamped))
    return directory


def produce(request, tools, env, spans):
    target = workspace(request.source)
    env["CARGO_TARGET_DIR"] = str(target)
    locked(request.source)
    listed, seconds = timed(tools.run, ["cargo", "metadata", "--locked", "--no-deps", "--format-version", "1"], request.source, env)
    spans.append(("metadata", seconds))
    package = owner(request.name, listed)
    spans.append(("fetch", timed(tools.stream, ["cargo", "fetch", "--locked", "--target", request.target], request.source, env)[1]))
    argv = ["cargo", "build", "--locked", "--offline", "--release", "--package", package, "--bin", request.name, "--target", request.target]
    spans.append(("build", timed(tools.stream, argv, request.source, env)[1]))
    built = target / request.target / "release" / f"{request.name}{request.suffix}"
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
    declared = tools.toolchain(request.source)
    channel = declared["channel"]
    spans = [("toolchain", timed(tools.stream, toolchain.install(declared, [request.target]), request.source)[1])]
    env = environment(request, channel)
    package = produce(request, tools, env, spans)
    print("\n".join(f"{name} {seconds}s" for name, seconds in spans), file=sys.stderr)
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
