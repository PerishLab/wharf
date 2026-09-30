import io
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.content import declaration, resources
from lib.process import git, run, unanchored
from lib.refusal import Refusal

HOST = resources.read_json("registries.json")["oci"]["host"]
CONTAINERFILE = "Containerfile"
PLATFORM = "linux/amd64"


@dataclass(frozen=True)
class Image:
    source: Path
    binary: Path
    name: str
    reference: str
    marker: str


@dataclass(frozen=True)
class SourceImage:
    source: Path
    reference: str


def reference(repository, version):
    owner, name = repository.lower().split("/", 1)
    return f"{HOST}/{owner}/{name}:{version}"


def exists(image, runner=subprocess.run):
    return runner(["docker", "manifest", "inspect", image], capture_output=True).returncode == 0


def carried(source):
    if declaration.release(source).get("oci") is None:
        return False
    if git(source, "ls-tree", "--name-only", "HEAD", "--", CONTAINERFILE) != CONTAINERFILE:
        raise Refusal(f"[release.oci] is declared and the product has no tracked {CONTAINERFILE}")
    return True


def context(source, binary, name):
    if not carried(source):
        raise Refusal("the product declares no image")
    if not Path(binary).is_file():
        raise Refusal(f"{binary} is missing")
    directory = Path(tempfile.mkdtemp())
    shutil.copy2(Path(source) / CONTAINERFILE, directory / CONTAINERFILE)
    shutil.copy2(binary, directory / name)
    (directory / name).chmod(0o755)
    return directory


def source_context(source):
    if not carried(source):
        raise Refusal("the product declares no image")
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


def public_digest(reference, runner=run):
    directory = Path(tempfile.mkdtemp())
    try:
        rendered = runner(
            ["docker", "--config", str(directory), "buildx", "imagetools", "inspect", "--format", "{{json .Manifest.Digest}}", reference],
            directory,
        )
        digest = json.loads(rendered)
    except (json.JSONDecodeError, subprocess.CalledProcessError) as failure:
        raise Refusal(f"{reference} is not anonymously readable by digest: {str(failure)[:500]}") from failure
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        raise Refusal(f"{reference} reported invalid public digest {digest!r}")
    return digest


def registry_digest(reference, runner=run):
    directory = Path(tempfile.mkdtemp())
    try:
        rendered = runner(
            ["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest.Digest}}", reference],
            directory,
        )
        digest = json.loads(rendered)
    except (json.JSONDecodeError, subprocess.CalledProcessError) as failure:
        raise Refusal(f"{reference} has no readable repository digest: {str(failure)[:500]}") from failure
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        raise Refusal(f"{reference} reported invalid repository digest {digest!r}")
    return digest


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


def publish(request, runner=run):
    image = request.reference
    if exists(image):
        return {"image": image, "state": "already-published"}
    directory = context(request.source, request.binary, request.name)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", CONTAINERFILE, "-t", image, "--label", f"org.opencontainers.image.version={version}", "."], directory)
        smoked(request, directory, runner)
        runner(["docker", "push", image], directory)
        digests = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if not exists(image):
        raise Refusal(f"{image} is not visible after pushing")
    return {"image": image, "state": "published", "digests": digests}


def publish_source(request, runner=run):
    image = request.reference
    repository = image.rsplit(":", 1)[0]
    if exists(image):
        expected = registry_digest(image, runner)
        digest = public_digest(image, runner)
        if digest != expected:
            raise Refusal(f"{image} publicly resolved to {digest}, expected {expected}")
        return {"image": image, "state": "already-published", "digests": [f"{repository}@{digest}"]}
    directory = source_context(request.source)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", CONTAINERFILE, "-t", image, "--label", f"org.opencontainers.image.version={version}", "."], directory)
        runner(["docker", "push", image], directory)
        local = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory))
        digest = public_digest(image, runner)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    published = f"{repository}@{digest}"
    if published not in local:
        raise Refusal(f"{image} resolved to {published}, which the pushed image does not carry")
    return {"image": image, "state": "published", "digests": [published]}
