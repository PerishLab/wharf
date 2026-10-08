import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from lib.content import preview
from lib.refusal import Refusal

TOOLS = ("node", "pnpm", "plumb", "ectropy", "git")
LIMIT = 1048576


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise Refusal("static document repeats a JSON field")
        result[key] = value
    return result


def decode(body):
    if len(body) > LIMIT:
        raise Refusal("static document exceeds its 1 MiB budget")
    try:
        return json.loads(body, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refusal("static document is not valid JSON") from error


def clean(environ, home):
    return {
        **{name: environ[name] for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT") if name in environ},
        "PATH": environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "CI": "true",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_IGNORE_PNPMFILE": "true",
    }


def tools(root, env, inspect, names=TOOLS):
    held = {}
    for name in names:
        found = shutil.which(name, path=env["PATH"])
        if found is None:
            raise Refusal(f"static build requires prepared {name}")
        path = Path(found).resolve()
        if path.is_relative_to(root) or not path.is_file():
            raise Refusal(f"static build {name} must be a trusted tool outside product source")
        held[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    env["PATH"] = os.pathsep.join(dict.fromkeys([str(Path(tool["path"]).parent) for tool in held.values()] + ["/usr/bin", "/bin"]))
    for name, tool in held.items():
        tool["version"] = inspect([tool["path"], "--version"], root, env).strip()
    return held


def unchanged(world):
    for name, tool in world.items():
        path = Path(tool["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != tool["sha256"]:
            raise Refusal(f"static build tool {name} changed during execution")


def inspect(argv, cwd, env):
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal("static tool invocation failed or exceeded 1800 seconds") from error
    if result.returncode != 0 or len(result.stdout) > LIMIT:
        raise Refusal("static tool refused or exceeded its output budget")
    return result.stdout


def guard(body, intent, version):
    held = decode(body)
    if not isinstance(held, dict) or held.get("schema") != "plumb.guard-runtime/v1" or held.get("ok") is not True or held.get("strength") != "full" or held.get("boundary") != "head" or held.get("commit") != intent["source"]["commit"]:
        raise Refusal("static build needs actual successful full/head Guard for this commit")
    if not isinstance(held.get("root"), str) or not (PurePosixPath(held["root"]).is_absolute() or PureWindowsPath(held["root"]).is_absolute()):
        raise Refusal("static Guard evidence must identify its absolute source root")
    proof = held.get("guard")
    if not isinstance(proof, dict) or proof.get("schema") != "plumb.guard-proof/v1" or proof.get("repository") != intent["repository"] or proof.get("tree") != intent["source"]["tree"] or not isinstance(proof.get("plumb"), str) or proof["plumb"].split("@")[0] != version.removeprefix("plumb "):
        raise Refusal("static Guard authority does not match repository, tree or invoked Plumb")
    for value in (held.get("digest"), proof.get("digest"), proof.get("depot")):
        preview.matches(value, preview.HEX[64], "Guard evidence digest")
    if not isinstance(proof.get("actions"), list) or not proof["actions"]:
        raise Refusal("static Guard evidence names no actions")
    for action in proof["actions"]:
        preview.shape(action, {"name", "input", "world"}, "Guard action")
        preview.text(action["name"], "Guard action name")
        for key in ("input", "world"):
            preview.matches(action[key], preview.HEX[64], "Guard action digest")
    return held
