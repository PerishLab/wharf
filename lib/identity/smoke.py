import json
import os
import subprocess
import tempfile
from pathlib import Path

from lib import resources
from lib.process import run
from lib.refusal import Refusal

SURFACES = ("--version", "--help")


def surface(executable, argument, runner):
    try:
        return runner([str(executable.resolve()), argument], executable.parent).strip()[:2000]
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[:500]
        raise Refusal(f"{executable.name} {argument} exited {failure.returncode}: {detail}")


def smoke(artifact, output, expect, runner=run):
    output = Path(output)
    if not artifact.file.is_file():
        raise Refusal(f"{artifact.file} is missing")
    if output.exists():
        raise Refusal(f"output {output} already exists")
    artifact.file.chmod(0o755)
    results = {argument: surface(artifact.file, argument, runner) for argument in SURFACES}
    if results["--version"] != expect:
        raise Refusal(f"{artifact.file.name} --version reported {results['--version']!r}, expected {expect!r}")
    output.mkdir(parents=True)
    receipt = {"action": "ship.binary.smoke", "expect": expect, "file": artifact.file.name, "surfaces": results}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def configured(artifact, repository, marker, runner=run):
    steps = resources.read_json("validators.json").get(repository, [])
    if not steps:
        return {"action": "ship.binary.configured", "steps": []}
    artifact.file.chmod(0o755)
    with tempfile.TemporaryDirectory() as home:
        env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for step in steps:
            argv = [str(artifact.file.resolve()), *(part.replace("{marker}", marker) for part in step)]
            try:
                runner(argv, home, env)
            except subprocess.CalledProcessError as failure:
                detail = (failure.stderr or failure.stdout or "").strip()[:500]
                raise Refusal(f"{' '.join(step)} exited {failure.returncode} in a clean home: {detail}")
    return {"action": "ship.binary.configured", "steps": [" ".join(step) for step in steps]}
