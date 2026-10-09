import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

NAMES = ("node", "pnpm", "plumb", "ectropy", "git")


def tools():
    result = {}
    for name in NAMES:
        path = Path(shutil.which(name)).resolve()
        if path.is_relative_to("/source"):
            raise RuntimeError("Preview tool resolves inside product source")
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True, timeout=1800).stdout.strip()
        result[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "version": version}
    return result


def execute(phase, package):
    before = tools()
    domain = json.loads(subprocess.run([before["plumb"]["path"], "metadata", "--json"], capture_output=True, text=True, check=True, timeout=1800).stdout)
    if any(before[name]["version"].lstrip("v") != domain[name + ".version"] for name in ("node", "pnpm")):
        raise RuntimeError("Preview guest tools differ from the trusted domain")
    commands = {
        "install": [before["pnpm"]["path"], "install", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile"],
        "guard": [before["plumb"]["path"], "guard", "--json", "/source"],
        "build": [before["pnpm"]["path"], "--filter", package, "run", "build"],
    }
    try:
        output = subprocess.run(commands[phase], cwd="/source", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if phase == "guard" else sys.stderr, stderr=sys.stderr, text=True, check=True, timeout=1800)
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, file=sys.stderr)
        raise
    if tools() != before:
        raise RuntimeError("Preview tools changed during product execution")
    return {"phase": phase, "tools": before, "guard": output.stdout if phase == "guard" else None}


if __name__ == "__main__":
    print(json.dumps(execute(sys.argv[1], sys.argv[2]), sort_keys=True))
