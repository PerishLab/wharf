"""Atomic action: run a fetched binary's version and help surfaces."""

import json
import subprocess
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship.binary import run


def smoke(directory, name, triple, output, runner=run):
    """Both surfaces must exit zero."""
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
