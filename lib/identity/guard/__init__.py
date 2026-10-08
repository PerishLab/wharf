import json
import os
import re
import subprocess
import tempfile

from lib.content import marker, resources
from lib.identity.bind import Artifact
from lib.process import run
from lib.refusal import Refusal

GUARD = resources.read_json("releases.json")["guard"]
FIELDS = {"producer", "depot"}
DEPOT = re.compile(r"[0-9a-f]{64}")


def guarded(repository):
    return repository in GUARD["repositories"]


def reported(release, bound, runner=run):
    if not guarded(release.repository):
        return None
    if GUARD["target"] not in bound:
        raise Refusal(f"{release.repository} seals its guard authority from its {GUARD['target']} binary, which this release does not hold")
    artifact = Artifact(bound[GUARD["target"]], release.repository.split("/", 1)[1], GUARD["target"])
    return checked(release, asked(artifact, runner))


def asked(artifact, runner):
    if not artifact.file.is_file():
        raise Refusal(f"{artifact.file} is missing")
    artifact.file.chmod(0o755)
    spoken = " ".join(GUARD["command"])
    with tempfile.TemporaryDirectory() as home:
        env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        try:
            output = runner([str(artifact.file.resolve()), *GUARD["command"]], home, env)
        except subprocess.CalledProcessError as failure:
            detail = (failure.stderr or failure.stdout or "").strip()[:500]
            raise Refusal(f"{artifact.file.name} {spoken} exited {failure.returncode}; a guard-authority release needs a binary that reports it: {detail}")
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise Refusal(f"{artifact.file.name} {spoken} did not print JSON: {output.strip()[:500]!r}")


def checked(release, held):
    if not isinstance(held, dict) or set(held) != FIELDS or not all(isinstance(value, str) for value in held.values()):
        raise Refusal(f"a guard authority holds exactly {', '.join(sorted(FIELDS))} as strings, not {held!r}")
    version, _, commit = held["producer"].partition("@")
    if not marker.holds(version) or version != release.marker:
        raise Refusal(f"the bound binary reports producer version {version!r}, this release is {release.marker}")
    if not commit or commit != release.commit:
        raise Refusal(f"the bound binary reports producer commit {commit!r}, this release is {release.commit}")
    if not DEPOT.fullmatch(held["depot"]):
        raise Refusal(f"the bound binary reports depot {held['depot']!r}, not 64 lowercase hex")
    return {"producer": held["producer"], "depot": held["depot"]}
