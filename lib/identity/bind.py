import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from lib.content import canonical
from lib.identity import image as located, region, signature
from lib.refusal import Refusal


@dataclass(frozen=True)
class Release:
    repository: str
    marker: str
    commit: str
    tree: str


@dataclass(frozen=True)
class Artifact:
    directory: Path
    name: str
    target: str

    @property
    def file(self):
        suffix = ".exe" if "windows" in self.target else ""
        return Path(self.directory) / f"{self.name}-{self.target}{suffix}"


def inspect(image):
    start, end = located.locate(image)
    return region.decode(image[start:end])


def bind(image, binding):
    start, end = located.locate(image)
    result = image[:start] + region.encode(image[start:end], binding) + image[end:]
    if inspect(result)[1] != binding:
        raise Refusal("bound executable identity did not read back")
    return result


def digest(release):
    return canonical.digest({"repository": release.repository, "marker": release.marker, "commit": release.commit, "tree": release.tree})


def executables(directory, target):
    suffix = f"-{target}{'.exe' if 'windows' in target else ''}"
    names = sorted(path.name[: -len(suffix)] for path in Path(directory).iterdir() if path.is_file() and path.name.endswith(suffix))
    if not names:
        raise Refusal(f"{directory} holds no executable built for {target}")
    return names


def performed(artifact, binding, output):
    if not artifact.file.is_file():
        raise Refusal(f"{artifact.file} is missing")
    bound = bind(artifact.file.read_bytes(), binding)
    origin, _ = inspect(bound)
    if origin["target"] != artifact.target:
        raise Refusal(f"executable was built for {origin['target']!r}, not {artifact.target}")
    target = output / artifact.file.name
    target.write_bytes(bound)
    shutil.copymode(artifact.file, target)
    signed = signature.finalize(target, artifact.target)
    final = target.read_bytes()
    if inspect(final)[1] != binding:
        raise Refusal("finalized executable identity did not read back")
    return {"file": target.name, "origin": origin, "signature": signed, "sha256": hashlib.sha256(final).hexdigest(), "size": len(final)}


def perform(built, release, workload, output):
    output = Path(output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    artifacts = [Artifact(built.directory, name, built.target) for name in executables(built.directory, built.target)]
    binding = {"product": built.name, "marker": release.marker, "digest": digest(release), "commit": release.commit, "workload": workload}
    output.mkdir(parents=True)
    held = [performed(artifact, binding, output) for artifact in artifacts]
    receipt = {"action": "ship.identity", "binding": binding, "binaries": held}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
