import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from lib.media import node
from lib.media.oci.registry import exists, public_digest, registry_digest
from lib.media.oci.spec import CONTAINERFILE, LABEL, PLATFORM, carried, tracked, untraversed, web_manifest
from lib.process import git, run, unanchored
from lib.refusal import Refusal

STRIPPED = frozenset({"NODE_AUTH_TOKEN", "NPM_CONFIG_USERCONFIG", "CLOUDFLARE_API_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"})


def context(source, binary, name, file=CONTAINERFILE):
    if not carried(source):
        raise Refusal("the product declares no image")
    containerfile = tracked(source, file, "file")
    if not Path(binary).is_file():
        raise Refusal(f"{binary} is missing")
    directory = Path(tempfile.mkdtemp())
    shutil.copy2(containerfile, directory / CONTAINERFILE)
    shutil.copy2(binary, directory / name)
    (directory / name).chmod(0o755)
    return directory


def source_context(source, file=CONTAINERFILE):
    if not carried(source):
        raise Refusal("the product declares no image")
    tracked(source, file, "file")
    directory = Path(tempfile.mkdtemp())
    archived = subprocess.run(
        ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
        capture_output=True,
        env=unanchored(),
    )
    if archived.returncode != 0:
        raise Refusal(f"git archive failed in {source}: {archived.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as packed:
        packed.extractall(directory, filter="data")
    return directory


def safe_dist(dist):
    resolved = dist.resolve()
    for current, dirs, files in os.walk(dist, followlinks=False):
        for name in dirs + files:
            entry = Path(current) / name
            if entry.is_symlink() and not entry.resolve().is_relative_to(resolved):
                raise Refusal(f"web dist symlink {entry} escapes {dist}")


def prepare_dist(source, web):
    rel = f"{web}/dist"
    dist = untraversed(source, rel, "web dist")
    if git(source, "ls-tree", "HEAD", "--", rel):
        raise Refusal(f"[release.oci] {rel} must not be tracked at HEAD")
    if dist.exists():
        if not dist.is_dir():
            raise Refusal(f"[release.oci] {rel} must be a directory")
        shutil.rmtree(dist)


def web_context(source, web, file):
    containerfile = tracked(source, file, "file")
    dist = untraversed(source, f"{web}/dist", "web dist")
    if not dist.is_dir() or not any(dist.iterdir()):
        raise Refusal(f"{web}/dist must exist and be non-empty after build")
    safe_dist(dist)
    directory = Path(tempfile.mkdtemp())
    try:
        shutil.copy2(containerfile, directory / CONTAINERFILE)
        shutil.copytree(dist, directory / "dist", symlinks=False)
    except OSError as failure:
        shutil.rmtree(directory, ignore_errors=True)
        raise Refusal(f"failed to assemble web build context: {failure}") from failure
    return directory


def sanitized(environ):
    base = os.environ if environ is None else environ
    return {
        key: value
        for key, value in base.items()
        if not key.upper().startswith("WHARF_R2_") and key.upper().removesuffix("_FILE") not in STRIPPED
    }


def invoke_node(argv, cwd, env, runner):
    try:
        runner(argv, cwd, env)
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[:500]
        raise Refusal(f"{' '.join(argv)} exited {failure.returncode}: {detail}") from failure


def build_web_bundle(request, runner):
    base = sanitized(request.env)
    with tempfile.TemporaryDirectory() as home:
        clean = dict(node.withheld(base), HOME=home, CI="true")
        flags = node.exclusions(request.source, lambda argv, cwd: runner(argv, cwd, clean))
    with tempfile.TemporaryDirectory() as auth:
        invoke_node(["pnpm", "install", "--frozen-lockfile", *flags], request.source, node.reading(auth, base), runner)
    with tempfile.TemporaryDirectory() as home:
        build_env = dict(node.withheld(base), HOME=home, CI="true")
        invoke_node(["pnpm", "--filter", request.package, "build"], request.source, build_env, runner)


def smoked(request, directory, runner):
    expect = f"{request.name} {request.marker}"
    try:
        reported = runner(["docker", "run", "--rm", "--platform", PLATFORM, request.reference, "--version"], directory).strip()
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[:500]
        raise Refusal(f"{request.reference} --version exited {failure.returncode}: {detail}")
    if reported != expect:
        raise Refusal(f"{request.reference} --version reported {reported[:500]!r}, expected {expect!r}")
    return reported


def web_labelled(image, version, directory, runner):
    try:
        rendered = runner(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image], directory)
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[:500]
        raise Refusal(f"{image} inspect exited {failure.returncode}: {detail}") from failure
    try:
        labels = json.loads(rendered) or {}
    except json.JSONDecodeError as failure:
        raise Refusal(f"{image} reported unreadable image labels") from failure
    actual = labels.get(LABEL) if isinstance(labels, dict) else None
    if actual != version:
        raise Refusal(f"{image} reported label {LABEL}={actual!r}, expected {version!r}")


def web_smoked(image, version, directory, runner):
    web_labelled(image, version, directory, runner)
    if not (directory / "dist" / ".perish" / "server.mjs").is_file():
        return
    try:
        runner(["docker", "run", "--rm", "--platform", PLATFORM, "--entrypoint", "node", image, "--check", "dist/.perish/server.mjs"], directory)
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[:500]
        raise Refusal(f"{image} node --check exited {failure.returncode}: {detail}") from failure


def hooks(probe):
    check = probe.exists if probe and probe.exists else exists
    public = probe.public_digest if probe and probe.public_digest else public_digest
    read = probe.registry_digest if probe and probe.registry_digest else registry_digest
    return check, public, read


def verify_existing(image, runner, confirm, probe):
    _, public, read = hooks(probe)
    if confirm is not None:
        confirm()
    expected = read(image, runner)
    if confirm is not None:
        confirm()
    digest = public(image, runner)
    if digest != expected:
        raise Refusal(f"{image} publicly resolved to {digest}, expected {expected}")
    return digest


def publish(request, runner=run, confirm=None, probe=None):
    check, _, _ = hooks(probe)
    image = request.reference
    if check(image):
        return {"image": image, "state": "already-published"}
    directory = context(request.source, request.binary, request.name, request.file)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", CONTAINERFILE, "-t", image, "--label", f"{LABEL}={version}", "."], directory)
        smoked(request, directory, runner)
        if confirm is not None:
            confirm()
        runner(["docker", "push", image], directory)
        digests = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if not check(image):
        raise Refusal(f"{image} is not visible after pushing")
    return {"image": image, "state": "published", "digests": digests}


def publish_source(request, runner=run, confirm=None, probe=None):
    check, public, _ = hooks(probe)
    image = request.reference
    repository = image.rsplit(":", 1)[0]
    if check(image):
        digest = verify_existing(image, runner, confirm, probe)
        return {"image": image, "state": "already-published", "digests": [f"{repository}@{digest}"]}
    directory = source_context(request.source, request.file)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", request.file, "-t", image, "--label", f"{LABEL}={version}", "."], directory)
        if confirm is not None:
            confirm()
        runner(["docker", "push", image], directory)
        local = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory))
        digest = public(image, runner)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    published = f"{repository}@{digest}"
    if published not in local:
        raise Refusal(f"{image} resolved to {published}, which the pushed image does not carry")
    return {"image": image, "state": "published", "digests": [published]}


def repo_digests(image, directory, runner):
    try:
        local = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory)) or []
    except (json.JSONDecodeError, subprocess.CalledProcessError) as failure:
        raise Refusal(f"{image} reported unreadable repository digests") from failure
    if not isinstance(local, list):
        raise Refusal(f"{image} reported malformed repository digests")
    return local


def publish_web(request, runner=run, confirm=None, probe=None):
    check, public, read = hooks(probe)
    image = request.reference
    repository = image.rsplit(":", 1)[0]
    if check(image):
        digest = verify_existing(image, runner, confirm, probe)
        return {"image": image, "digest": digest, "status": "already-published"}
    tracked(request.source, request.file, "file")
    package = web_manifest(request.source, request.web, verify=True)
    if package != request.package:
        raise Refusal(f"[release.oci] web package {request.package!r} differs from manifest {package!r}")
    prepare_dist(request.source, request.web)
    build_web_bundle(request, runner)
    directory = web_context(request.source, request.web, request.file)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", CONTAINERFILE, "-t", image, "--label", f"{LABEL}={version}", "."], directory)
        web_smoked(image, version, directory, runner)
        if confirm is not None:
            confirm()
        runner(["docker", "push", image], directory)
        local = repo_digests(image, directory, runner)
        expected = read(image, runner)
        digest = public(image, runner)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if digest != expected:
        raise Refusal(f"{image} publicly resolved to {digest}, expected {expected}")
    published = f"{repository}@{digest}"
    if published not in local:
        raise Refusal(f"{image} resolved to {published}, which the pushed image does not carry")
    return {"image": image, "digest": digest, "status": "published"}
