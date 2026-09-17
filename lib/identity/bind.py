import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from lib import canonical
from lib.identity import elf, region
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
    start, end = elf.locate(image)
    return region.decode(image[start:end])


def bind(image, binding):
    start, end = elf.locate(image)
    result = image[:start] + region.encode(image[start:end], binding) + image[end:]
    if inspect(result)[1] != binding:
        raise Refusal("bound executable identity did not read back")
    return result


def digest(release):
    return canonical.digest({"repository": release.repository, "marker": release.marker, "commit": release.commit, "tree": release.tree})


def perform(artifact, release, workload, output):
    output = Path(output)
    if not artifact.file.is_file():
        raise Refusal(f"{artifact.file} is missing")
    if output.exists():
        raise Refusal(f"output {output} already exists")
    binding = {"product": artifact.name, "marker": release.marker, "digest": digest(release), "commit": release.commit, "workload": workload}
    bound = bind(artifact.file.read_bytes(), binding)
    origin, _ = inspect(bound)
    if origin["target"] != artifact.target:
        raise Refusal(f"executable was built for {origin['target']!r}, not {artifact.target}")
    output.mkdir(parents=True)
    target = output / artifact.file.name
    target.write_bytes(bound)
    shutil.copymode(artifact.file, target)
    receipt = {"action": "ship.identity", "file": target.name, "binding": binding, "origin": origin, "sha256": hashlib.sha256(bound).hexdigest(), "size": len(bound)}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
