import hashlib
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

from lib.cargo import index, publish, registry, suite, toolchain
from lib.content import declaration, marker
from lib.content.static import evidence
from lib.identity.guard.exercise import MACHINE
from lib.media import release
from lib.process import git, run
from lib.refusal import Refusal
from lib.store import distribution

TARGET = "x86_64-pc-windows-msvc"


def identity(held):
    source = Path(held["source"]).resolve()
    version = held["marker"]
    if marker.channel(version) != "rc":
        raise Refusal("library verification requires an exact RC marker")
    origin = git(source, "remote", "get-url", "origin")
    if origin not in {f"https://github.com/{held['repository']}", f"https://github.com/{held['repository']}.git"}:
        raise Refusal("library verification checkout has a foreign repository")
    commit = git(source, "rev-parse", "HEAD")
    if git(source, "rev-parse", f"refs/tags/{version}^{{commit}}") != commit or git(source, "status", "--porcelain"):
        raise Refusal("library verification needs a clean exact-marker checkout")
    declared = declaration.release(source)
    if declared.get("binaries") or not publish.publishable(source):
        raise Refusal("library verification needs published Cargo libraries without binaries")
    return {"repository": held["repository"], "marker": version, "commit": commit, "tree": git(source, "rev-parse", "HEAD^{tree}")}


def ready(expected):
    authority = release.place(release.Release(expected["repository"], expected["marker"], expected["commit"], ""))[2]
    body = registry.fetch_bytes(f"{authority}/{distribution.location(expected['marker'])}")
    found = evidence.decode(body)
    if found.get("schema") != 1 or found.get("state") != "complete" or found.get("media", {}).get("cargo") != "published":
        raise Refusal("library verification needs complete Cargo distribution")
    if any(found.get(name) != value for name, value in expected.items()):
        raise Refusal("library distribution identity differs from its checkout")
    return hashlib.sha256(body).hexdigest()


def packaged(source, expected, name):
    package, known = name
    location = registry.declared(source, known)
    version = marker.version(expected["marker"])
    line = registry.record(location, package, version)
    if line is None or line.get("yanked") is not False:
        raise Refusal("library RC package is missing or yanked")
    blob = registry.fetch_bytes(location + registry.download(location, line))
    if hashlib.sha256(blob).hexdigest() != line["cksum"]:
        raise Refusal("library RC package differs from its index checksum")
    manifest = index.manifest(blob, package, version)
    identity = manifest.get("package", {})
    bound = identity.get("metadata", {}).get("perish", {}).get("release")
    if identity.get("name") != package or identity.get("version") != version or bound != {"schema": 1, **expected}:
        raise Refusal("library RC package has foreign release metadata")
    return blob, {"package": package, "version": version, "sha256": line["cksum"], "release": bound}


def unpack(blob, destination, package):
    prefix = f"{package['package']}-{package['version']}"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith(prefix + "/"):
                raise Refusal("library RC archive needs regular package files")
        archive.extractall(destination, filter="data")
    return destination / prefix


def environment(base, channel):
    env = evidence.clean(os.environ, base)
    env.update({name: os.environ[name] for name in MACHINE if name in os.environ})
    env.update({"USERPROFILE": str(base), "TEMP": str(base), "TMP": str(base), "CARGO_HOME": str(base / "cargo"), "CARGO_TARGET_DIR": str(base / "target"), "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")), "RUSTUP_TOOLCHAIN": channel, "CARGO_TERM_COLOR": "never"})
    return env


def execute(root, env):
    lock = root / "Cargo.lock"
    before = hashlib.sha256(lock.read_bytes()).hexdigest()
    code, log = suite.attempt(["cargo", "test", "--locked", "--all-features", "--no-fail-fast"], root, env)
    targets, totals = suite.tally(log)
    if code or not targets or totals["failed"] or totals["ignored"]:
        raise Refusal(f"library RC all-features tests failed or omitted coverage: {totals}")
    if hashlib.sha256(lock.read_bytes()).hexdigest() != before:
        raise Refusal("library RC tests changed their packaged lock")
    return {"command": ["cargo", "test", "--locked", "--all-features", "--no-fail-fast"], "lockSha256": before, "targets": targets, "totals": totals, "logSha256": hashlib.sha256(log.encode()).hexdigest()}


def case(held):
    expected = identity(held)
    proof = ready(expected)
    versions = toolchain.checked(json.loads(held["domain"]))
    results = []
    with tempfile.TemporaryDirectory(prefix="wharf-library-") as directory:
        base = Path(directory)
        env = environment(base, versions["rust.version"])
        rustc = run(["rustc", "-vV"], base, env).strip()
        if f"host: {TARGET}" not in rustc.splitlines() or not rustc.startswith(f"rustc {versions['rust.version']} "):
            raise Refusal("library verification requires native Windows and the exact domain Rust")
        for name in publish.publishable(held["source"]):
            blob, package = packaged(held["source"], expected, name)
            root = unpack(blob, base, package)
            results.append({**package, **execute(root, env)})
    if not sum(item["totals"]["passed"] for item in results):
        raise Refusal("library verification executed no tests")
    if Path(directory).exists():
        raise Refusal("library verification cleanup is unknown")
    if identity(held) != expected or ready(expected) != proof:
        raise Refusal("library verification release evidence changed during execution")
    return {"schema": "wharf.library-rc/v1", "source": expected, "distributionSha256": proof, "domain": versions, "target": TARGET, "rustc": rustc, "packages": results, "cleanup": True}
