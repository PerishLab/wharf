import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import toolchain
from lib.process import run
from lib.refusal import Refusal

TIMEOUT = 1800
RUNNING = re.compile(r"^\s+Running (?:unittests )?(\S+) \((?:.*[/\\])?([^/\\]+?)-[0-9a-f]+(?:\.exe)?\)")
DOCTESTS = re.compile(r"^\s+Doc-tests (\S+)")
RESULT = re.compile(r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored")


@dataclass(frozen=True)
class Suite:
    source: Path
    output: Path


def attempt(argv, cwd, env):
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refusal(f"the test suite exceeded {TIMEOUT}s")
    return done.returncode, done.stdout


@dataclass(frozen=True)
class Tools:
    run: object = run
    attempt: object = attempt
    toolchain: object = toolchain.declared


def isolated(channel, home, target):
    base = Path.home()
    env = {name: value for name, value in os.environ.items() if not name.startswith(("CARGO_", "RUSTUP_"))}
    env.update(
        HOME=str(home),
        CARGO_HOME=os.environ.get("CARGO_HOME", str(base / ".cargo")),
        RUSTUP_HOME=os.environ.get("RUSTUP_HOME", str(base / ".rustup")),
        RUSTUP_TOOLCHAIN=channel,
        CARGO_TARGET_DIR=str(target),
        CARGO_TERM_COLOR="never",
    )
    return env


def tally(log):
    targets, current = [], None
    for line in log.splitlines():
        running = RUNNING.match(line)
        doctests = DOCTESTS.match(line)
        if running or doctests:
            current = f"{running.group(2)} {running.group(1)}" if running else f"{doctests.group(1)} doctests"
            continue
        result = RESULT.match(line)
        if result:
            state, passed, failed, ignored = result.groups()
            targets.append({"target": current, "state": state, "passed": int(passed), "failed": int(failed), "ignored": int(ignored)})
    totals = {field: sum(item[field] for item in targets) for field in ("passed", "failed", "ignored")}
    return targets, totals


def execute(request, tools, channel):
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as target:
        env = isolated(channel, home, target)
        argv = ["cargo", "test", "--workspace", "--locked", "--no-fail-fast"]
        code, log = tools.attempt(argv, request.source, env)
        rustc = tools.run(["rustc", "--version"], request.source, env).strip()
    return code, log, rustc


def suite(request, tools=Tools()):
    request = Suite(Path(request.source).resolve(), Path(request.output))
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    channel = tools.toolchain(request.source)["channel"]
    tools.run(["rustup", "toolchain", "install", channel, "--profile", "minimal"], request.source)
    code, log, rustc = execute(request, tools, channel)
    print(log, file=sys.stderr)
    targets, totals = tally(log)
    if code != 0 or totals["failed"] or not targets:
        failing = [item["target"] for item in targets if item["state"] != "ok"]
        raise Refusal(f"cargo test exited {code}; failing targets {failing}; totals {totals}")
    request.output.mkdir(parents=True)
    receipt = {
        "action": "ship.cargo.suite",
        "toolchain": channel,
        "rustc": rustc,
        "targets": targets,
        "totals": totals,
    }
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
