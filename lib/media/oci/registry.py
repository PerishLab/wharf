import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.content import marker
from lib.media.oci.spec import CHANNEL, LABEL, PLATFORM
from lib.process import run
from lib.refusal import Refusal


@dataclass(frozen=True)
class Probe:
    exists: object = None
    public_digest: object = None
    registry_digest: object = None
    labelled: object = None


def exists(image, runner=subprocess.run):
    return runner(["docker", "manifest", "inspect", image], capture_output=True).returncode == 0


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


def channeled(image):
    return f"{image.rsplit(':', 1)[0]}:{CHANNEL}"


def released(image):
    return "v" + image.rsplit(":", 1)[1]


def labelled(reference, runner=run):
    directory = Path(tempfile.mkdtemp())
    try:
        rendered = runner(
            ["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Image}}", reference],
            directory,
        )
    except subprocess.CalledProcessError as failure:
        if "not found" in (failure.stderr or ""):
            return None
        raise Refusal(f"{reference} cannot be read: {(failure.stderr or failure.stdout or '').strip()[:500]}") from failure
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    try:
        held = json.loads(rendered)
    except json.JSONDecodeError as failure:
        raise Refusal(f"{reference} reported an unreadable image configuration") from failure
    held = held if "config" in held else held.get(PLATFORM, {})
    version = (held.get("config") or {}).get("Labels", {}).get(LABEL)
    if not isinstance(version, str) or not marker.holds(f"v{version}"):
        raise Refusal(f"{reference} carries no release version in {LABEL}: {version!r}")
    return version


def current(image, runner=run, probe=None):
    held = released(image)
    if marker.channel(held) != CHANNEL:
        return True
    inspect = probe.labelled if probe and probe.labelled else labelled
    version = inspect(channeled(image), runner)
    return version is not None and marker.order(f"v{version}") >= marker.order(held)


def advance(image, runner=run, confirm=None, probe=None):
    held = released(image)
    if marker.channel(held) != CHANNEL:
        return None
    inspect = probe.labelled if probe and probe.labelled else labelled
    resolved = probe.registry_digest if probe and probe.registry_digest else registry_digest
    target = channeled(image)
    version = inspect(target, runner)
    if version is not None and marker.order(f"v{version}") > marker.order(held):
        return {"image": target, "state": "kept", "version": version}
    digest = resolved(image, runner)
    if version is not None and f"v{version}" == held and resolved(target, runner) == digest:
        return {"image": target, "state": "kept", "version": version}
    directory = Path(tempfile.mkdtemp())
    try:
        if confirm is not None:
            confirm()
        runner(["skopeo", "copy", "--preserve-digests", f"docker://{image.rsplit(':', 1)[0]}@{digest}", f"docker://{target}"], directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    moved = resolved(target, runner)
    if moved != digest:
        raise Refusal(f"{target} resolved to {moved} after moving, expected {digest}")
    return {"image": target, "state": "advanced", "version": held.removeprefix("v"), "digest": digest}
