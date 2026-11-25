import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lib.refusal import Refusal

REPOSITORY = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)

ROUTES = (
    (r"", lambda owner, name, _: ["repository", owner, name]),
    (r"branches/([^/?]+)", lambda owner, name, match: ["branch", owner, name, match.group(1)]),
    (r"compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", lambda owner, name, match: ["lifecycle", "ancestry", owner, name, match.group(1), match.group(2)]),
    (r"git/ref/tags/([^?]+)", lambda owner, name, match: ["ref", owner, name, "tag", match.group(1)]),
    (r"git/tags/([0-9a-f]{40})", lambda owner, name, match: ["annotation", owner, name, match.group(1)]),
    (r"git/commits/([0-9a-f]{40})", lambda owner, name, match: ["commit", owner, name, match.group(1)]),
    (r"contents/([^?]+)\?ref=([0-9a-f]{40})", lambda owner, name, match: ["contents", owner, name, match.group(2), match.group(1)]),
    (r"actions/runs/([1-9][0-9]*)", lambda owner, name, match: ["workflow", owner, name, match.group(1)]),
    (r"actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)", lambda owner, name, match: ["workflow", owner, name, match.group(1), "--attempt", match.group(2)]),
    (r"actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)/jobs\?per_page=([1-9][0-9]*)", lambda owner, name, match: ["jobs", owner, name, match.group(1), "--attempt", match.group(2), "--size", match.group(3)]),
)


def run(argv, cwd, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def stream(argv, cwd, env=None):
    sys.stderr.flush()
    subprocess.run(argv, cwd=cwd, env=env, check=True, stdout=sys.stderr, stderr=subprocess.STDOUT)


def unanchored():
    return {name: value for name, value in os.environ.items() if name not in REPOSITORY}


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, env=unanchored())
    if result.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {source}: {result.stderr.strip()}")
    return result.stdout.strip()


def route(repository, path):
    owner, name = repository.split("/", 1)
    for pattern, build in ROUTES:
        matched = re.fullmatch(pattern, path)
        if matched is not None:
            return build(owner, name, matched)
    raise Refusal(f"unsupported GitHub API route: {path}")


def endpoint(path):
    matched = re.fullmatch(r"repos/([^/?]+/[^/?]+)(?:/(.*))?", path)
    if matched is None:
        raise Refusal(f"unsupported GitHub API endpoint: {path}")
    return route(matched.group(1), matched.group(2) or "")


def github(arguments, timeout=30, limit=None):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise Refusal("GitHub token is missing")
    with tempfile.TemporaryDirectory(prefix="wharf-github-") as directory:
        path = Path(directory) / "token"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
        raw = subprocess.check_output(["runseal", ":", "@github", "--token-file", str(path), "--json", *arguments], stderr=subprocess.PIPE, timeout=timeout)
    if limit is not None and not 0 < len(raw) <= limit:
        raise Refusal("GitHub response exceeds bounds")
    reply = json.loads(raw)
    held = reply.get("github") if isinstance(reply, dict) else None
    if not isinstance(held, dict) or reply.get("version") != 1 or held.get("outcome") != "observed" or "value" not in held:
        raise ValueError("GitHub provider response is malformed")
    return held["value"]
