import json
import os
import re
import subprocess

from lib.process import run
from lib.refusal import Refusal

VARIABLE = "WHARF_DOMAIN"
KEYS = ("node.version", "pnpm.version", "rust.version")
EXACT = re.compile(r"\d+\.\d+\.\d+")
PROFILE = "minimal"
COMPONENTS = ["clippy", "rustfmt"]


def checked(held):
    if not isinstance(held, dict):
        raise Refusal("plumb metadata did not report an object of domain versions")
    found = {key: held.get(key) for key in KEYS}
    wrong = sorted(key for key, value in found.items() if not isinstance(value, str) or not EXACT.fullmatch(value))
    if wrong:
        raise Refusal(f"plumb metadata reports no exact x.y.z for {', '.join(wrong)}")
    return found


def resolve(runner=run):
    try:
        held = json.loads(runner(["plumb", "metadata", "--json"], "."))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as failure:
        raise Refusal(f"plumb metadata could not be read: {failure}") from failure
    return checked(held)


def encoded(versions):
    return json.dumps(versions, sort_keys=True, separators=(",", ":"))


def versions(env=None):
    text = (os.environ if env is None else env).get(VARIABLE, "")
    if not text:
        raise Refusal(f"{VARIABLE} is not set; the plan job resolves the domain versions from plumb metadata once per run")
    try:
        held = json.loads(text)
    except json.JSONDecodeError as failure:
        raise Refusal(f"{VARIABLE} is not JSON: {failure}") from failure
    return checked(held)


def current():
    return {"channel": versions()["rust.version"], "profile": PROFILE, "components": list(COMPONENTS), "targets": []}


def install(declared, targets=()):
    argv = ["rustup", "toolchain", "install", declared["channel"], "--profile", declared["profile"]]
    for component in declared["components"]:
        argv += ["--component", component]
    for target in sorted({*declared["targets"], *targets}):
        argv += ["--target", target]
    return argv
