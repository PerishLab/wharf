import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.process import git, run
from lib.refusal import Refusal

MANIFEST = "package.json"
EXACT = re.compile(r"\d+\.\d+\.\d+")
TOOLS = ("node", "pnpm")
TIMEOUT = 1800
WIDENED = ["the whole repository tree, because the pnpm workspace resolves across packages"]


@dataclass(frozen=True)
class Suite:
    source: Path
    output: Path


def manifest(source, path=MANIFEST):
    if git(source, "ls-tree", "--name-only", "HEAD", "--", path) != path:
        raise Refusal(f"{path} is not tracked at HEAD")
    return json.loads((Path(source) / path).read_text())


def declared(source):
    root = manifest(source)
    if "packageManager" in root:
        raise Refusal("package.json must not declare packageManager; declare exact engines instead")
    engines = root.get("engines", {})
    found = {tool: engines.get(tool, "") for tool in TOOLS}
    for tool, version in found.items():
        if not EXACT.fullmatch(version):
            raise Refusal(f"package.json engines.{tool} {version!r} must be an exact x.y.z version")
    return found


def basis(source, runner):
    return {
        "entry": {"kind": "node-suite", "scope": "workspace", "runner": runner},
        "engines": declared(source),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": WIDENED,
    }


def prepared(source, runner=run):
    expected = declared(source)
    held = {tool: runner([tool, "--version"], source).strip().lstrip("v") for tool in TOOLS}
    if held != expected:
        raise Refusal(f"prepared toolchain {held} differs from declared engines {expected}")
    return held


def attempt(argv, cwd, env):
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refusal(f"{' '.join(argv)} exceeded {TIMEOUT}s")
    print(done.stdout, file=sys.stderr)
    if done.returncode != 0:
        raise Refusal(f"{' '.join(argv)} exited {done.returncode}")


def suite(request, runner=run, execute=attempt):
    request = Suite(Path(request.source).resolve(), Path(request.output))
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    held = prepared(request.source, runner)
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, CI="true")
        env.pop("NODE_AUTH_TOKEN", None)
        execute(["pnpm", "install", "--frozen-lockfile"], request.source, env)
        execute(["pnpm", "-r", "test"], request.source, env)
    request.output.mkdir(parents=True)
    receipt = {"action": "ship.node.suite", "toolchain": held, "commands": ["pnpm install --frozen-lockfile", "pnpm -r test"]}
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
