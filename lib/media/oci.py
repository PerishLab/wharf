import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.content import resources
from lib.process import git, run
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


def reference(repository, version):
    owner, name = repository.lower().split("/", 1)
    return f"{HOST}/{owner}/{name}:{version}"


def exists(image, runner=subprocess.run):
    return runner(["docker", "manifest", "inspect", image], capture_output=True).returncode == 0


def carried(source):
    return git(source, "ls-tree", "--name-only", "HEAD", "--", CONTAINERFILE) == CONTAINERFILE


def context(source, binary, name):
    if not carried(source):
        raise Refusal(f"the product has no tracked {CONTAINERFILE}")
    if not Path(binary).is_file():
        raise Refusal(f"{binary} is missing")
    directory = Path(tempfile.mkdtemp())
    shutil.copy2(Path(source) / CONTAINERFILE, directory / CONTAINERFILE)
    shutil.copy2(binary, directory / name)
    (directory / name).chmod(0o755)
    return directory


def publish(request, runner=run):
    image = request.reference
    if exists(image):
        return {"image": image, "state": "already-published"}
    directory = context(request.source, request.binary, request.name)
    try:
        version = image.rsplit(":", 1)[1]
        runner(["docker", "build", "--pull", "--platform", PLATFORM, "-f", CONTAINERFILE, "-t", image, "--label", f"org.opencontainers.image.version={version}", "."], directory)
        runner(["docker", "push", image], directory)
        digests = json.loads(runner(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], directory))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if not exists(image):
        raise Refusal(f"{image} is not visible after pushing")
    return {"image": image, "state": "published", "digests": digests}
