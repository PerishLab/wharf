import json
import os
import signal
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import toolchain
from lib.content import resources
from lib.content.static import assets, evidence, source as static
from lib.process import git, run
from lib.refusal import Refusal
from lib.store import handoff

MANIFEST = "package.json"
TOOLS = ("node", "pnpm")
TIMEOUT = 1800
WIDENED = ["the whole repository tree, because the pnpm workspace resolves across packages"]
READER = "WHARF_PACKAGES_TOKEN"
REGISTRIES = resources.read_json("registries.json")["npm"]


@dataclass(frozen=True)
class Suite:
    source: Path
    output: Path


def carried(source, path=MANIFEST):
    return git(source, "ls-tree", "--name-only", "HEAD", "--", path) == path


def manifest(source, path=MANIFEST):
    if not carried(source, path):
        raise Refusal(f"{path} is not tracked at HEAD")
    return json.loads((Path(source) / path).read_text())


def expected(source):
    if "packageManager" in manifest(source):
        raise Refusal("package.json must not declare packageManager; Plumb carries the domain versions")
    held = toolchain.versions()
    return {tool: held[f"{tool}.version"] for tool in TOOLS}


def basis(source, runner):
    return {
        "entry": {"kind": "node-suite", "scope": "workspace", "runner": runner},
        "engines": expected(source),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": WIDENED,
    }


def prepared(source, runner=run):
    wanted = expected(source)
    held = {tool: runner([tool, "--version"], source).strip().lstrip("v") for tool in TOOLS}
    if held != wanted:
        raise Refusal(f"prepared toolchain {held} differs from the domain versions {wanted}")
    return held


def reading(directory, env):
    if not env.get(READER):
        return env
    path = Path(directory) / "npmrc"
    lines = [f"//{urllib.parse.urlsplit(location).netloc}/:_authToken=${{{READER}}}" for location in sorted(set(REGISTRIES.values()))]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return dict(env, NPM_CONFIG_USERCONFIG=str(path))


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
        execute(["pnpm", "install", "--frozen-lockfile"], request.source, reading(home, env))
        env.pop(READER, None)
        execute(["pnpm", "-r", "test"], request.source, env)
    request.output.mkdir(parents=True)
    receipt = {"action": "ship.node.suite", "toolchain": held, "commands": ["pnpm install --frozen-lockfile", "pnpm -r test"]}
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


@dataclass(frozen=True)
class Preview:
    source: Path
    output: Path
    intent: dict
    target: dict


def preview_commands(held, execute, inspect):
    root, tools, env, config = (held[key] for key in ("root", "tools", "env", "config"))
    installation = reading(held["install"], dict(env))
    if held.get("reader"):
        installation[READER] = held["reader"]
        installation = reading(held["install"], installation)
    execute([tools["pnpm"]["path"], "install", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile"], root, installation)
    qualified = static.qualify(root, held["request"].intent, held["request"].target, env)
    if qualified != config:
        raise Refusal("static source changed during installation")
    guarded = dict(env, PLUMB_GUARD_STRENGTH="full", PLUMB_GUARD_BOUNDARY="head")
    body = inspect([tools["plumb"]["path"], "guard", "--json", str(root)], root, guarded)
    proof = evidence.guard(body, held["request"].intent, tools["plumb"]["version"])
    if Path(proof.get("root", "")).resolve() != root:
        raise Refusal("static Guard observed a different source root")
    evidence.unchanged(tools)
    execute([tools["pnpm"]["path"], "--filter", config["package"]["name"], "run", "build"], root, env)
    if static.qualify(root, held["request"].intent, held["request"].target, env) != config:
        raise Refusal("static source changed during build")
    evidence.unchanged(tools)
    return proof


def preview_build(request, execute=None, inspect=evidence.inspect, environ=os.environ):
    execute = execute or preview_execute
    domain = toolchain.versions(environ)
    root, destination = Path(request.source).resolve(), Path(request.output).resolve()
    if destination.exists() or destination.is_relative_to(root):
        raise Refusal("static build handoff must be an absent directory outside product source")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as installation:
        env = evidence.clean(environ, home)
        tools = evidence.tools(root, env, inspect)
        config = static.qualify(root, request.intent, request.target, env)
        static.fresh(root, env)
        generated = static.output(root, config["directory"])
        if generated.exists():
            raise Refusal("static build assets directory already exists")
        if static.git(root, ["check-ignore", "--", config["directory"] + "/"], env).rstrip("/") != config["directory"]:
            raise Refusal("static assets directory must be ignored build output")
        for name in TOOLS:
            if tools[name]["version"].lstrip("v") != domain[f"{name}.version"]:
                raise Refusal("static prepared Node/pnpm differs from the trusted domain versions")
        identity = handoff.world()
        held = {"root": root, "tools": tools, "env": env, "config": config, "request": request, "install": installation, "reader": environ.get(READER)}
        proof = preview_commands(held, execute, inspect)
        if toolchain.versions(environ) != domain:
            raise Refusal("static domain versions changed during execution")
        if handoff.world() != identity:
            raise Refusal("static build implementation changed during execution")
        files = assets.collect(static.output(root, config["directory"]))
        origin = dict(request.intent["source"], repository=request.intent["repository"])
        receipt = {"schema": "wharf.preview.build/v1", "source": origin, "app": request.intent["app"], "guard": proof, "tools": tools, "implementation": identity}
        return handoff.pack(destination, files, receipt)


def preview_execute(argv, cwd, env):
    if os.name != "posix":
        raise Refusal("static build requires the dedicated POSIX runner")
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=sys.stderr, stderr=subprocess.STDOUT, start_new_session=True)
        if process.wait(timeout=TIMEOUT) != 0:
            raise Refusal("static product command exited unsuccessfully")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal("static product command refused or exceeded its execution budget") from error
    finally:
        preview_stop(process)


def preview_stop(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
