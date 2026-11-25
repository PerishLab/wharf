import base64
import functools
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from lib import process
from lib.content import canonical
from lib.media import node
from lib.process import git
from lib.process.follow import event
from lib.refusal import Refusal

TIMEOUT = 1800


def ownership(path, record):
    body = canonical.encode(record)
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != body:
            raise Refusal("follow workspace ownership disagrees; preserve the existing payload")
        return
    with path.open("xb") as held:
        held.write(body)


def seat(root, summary):
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".wharf-follow.json"
    if not marker.exists() and any(root.iterdir()):
        raise Refusal("follow data directory is not empty or owned")
    ownership(marker, {"schema": "wharf.follow.workspace/v1", "owner": "PerishLab/wharf"})
    source = root / "repositories" / summary["repository"]
    owned = root / "ownership" / (summary["repository"] + ".json")
    if any(path.resolve() != path for path in (source, owned, root / "plumb")):
        raise Refusal("follow workspace paths contain aliases; preserve them")
    source.parent.mkdir(parents=True, exist_ok=True)
    owned.parent.mkdir(parents=True, exist_ok=True)
    if source.exists() and not owned.exists():
        raise Refusal("follow source exists without ownership; preserve it")
    ownership(owned, {"schema": "wharf.follow.source/v1", "repository": summary["repository"], "id": summary["repository_id"]})
    return source


def fault(raw):
    try:
        held = json.loads(raw)
    except (ValueError, UnicodeError):
        return None, None
    error = held.get("error") if isinstance(held, dict) else None
    if not isinstance(error, dict):
        return None, None
    evidence = error.get("evidence")
    status = evidence.get("status") if isinstance(evidence, dict) else None
    return error.get("outcome"), status


def api(arguments, token, optional=False):
    try:
        return process.github(arguments, timeout=30, limit=event.LIMIT, token=token)
    except subprocess.CalledProcessError as error:
        outcome, status = fault(error.output or b"")
        if optional and outcome == "refused" and status == 404:
            return None
        if outcome == "refused":
            raise Refusal("follow repository qualification was refused by GitHub") from error
        raise Refusal("follow repository qualification is unknown") from error
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Refusal("follow repository qualification is unknown") from error


def governed(summary, token, reader=api):
    repository = summary["repository"]
    owner, name = repository.split("/", 1)
    held = reader(["repository", owner, name], token)
    if not isinstance(held, dict) or (held.get("id"), held.get("full_name"), held.get("default_branch")) != (summary["repository_id"], repository, "main") or held.get("archived") is not False:
        raise Refusal("follow repository identity or main differs from the signed push")
    pointer = reader(["ref", owner, name, "branch", "main"], token)
    commit = pointer.get("object", {}).get("sha") if isinstance(pointer, dict) and isinstance(pointer.get("object"), dict) else None
    if not isinstance(commit, str) or not event.OBJECT.fullmatch(commit):
        raise Refusal("follow repository identity or main differs from the signed push")
    declared = reader(["contents", owner, name, commit, "plumb.toml"], token, True)
    if declared is None:
        return False
    if not isinstance(declared, dict) or declared.get("commit") != commit or declared.get("path") != "plumb.toml" or not isinstance(declared.get("base64"), str):
        raise Refusal("follow needs the tracked root Plumb declaration")
    return True


def remote(repository):
    return f"https://github.com/{repository}.git"


def authenticate(config, repository, token):
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(f'[http]\n\tfollowRedirects = false\n[http "{remote(repository)}"]\n\textraHeader = Authorization: Basic {header}\n')


def environment(home, root, repository, environ):
    names = ("PATH", "LANG", "LC_ALL", "CI", "TMPDIR", "TMP", "TEMP", "WHARF_DOMAIN", "SSL_CERT_FILE", "SSL_CERT_DIR", "RUNNER_TRACKING_ID")
    held = {name: environ[name] for name in names if name in environ}
    config = home / "gitconfig"
    token = environ["WHARF_FOLLOW_TOKEN"]
    held.update({"HOME": str(home), "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GITHUB_TOKEN": token, "PLUMB_HOME": str(root / "plumb"), "RUSTUP_HOME": environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")), "CARGO_HOME": environ.get("CARGO_HOME", str(Path.home() / ".cargo"))})
    if environ.get(node.READER):
        held[node.READER] = environ[node.READER]
    held = node.reading(home, held, literal=True)
    authenticate(config, repository, token)
    return held


def cancelled():
    raise Refusal("follow was cancelled; recover Auto state from GitHub")


def received(held, number, frame):
    held["received"] = True
    if held["started"]:
        cancelled()


@contextmanager
def cancellation():
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    held = {"started": False, "received": False}
    try:
        for number in previous:
            signal.signal(number, functools.partial(received, held))
        yield held
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def reap(process):
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, signal.SIG_IGN)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        raise Refusal("follow termination is unknown; recover from GitHub") from error


def command(argv, cwd, env):
    print(json.dumps({"command": argv}), file=sys.stderr)
    with cancellation() as held:
        process = None
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=sys.stderr, stderr=subprocess.STDOUT, start_new_session=True)
            held["started"] = True
            if held["received"]:
                cancelled()
            if process.wait(timeout=TIMEOUT) != 0:
                raise Refusal("follow step failed; recover any existing Auto state from GitHub")
        except subprocess.TimeoutExpired as error:
            raise Refusal("released follow exceeded its finite execution budget; recover from GitHub") from error
        finally:
            if process is not None:
                reap(process)


def synchronize(source, summary, env):
    upstream = remote(summary["repository"])
    if not source.exists():
        command(["git", "clone", "--single-branch", "--branch", "main", "--no-tags", upstream, str(source)], source.parent, env)
    if source.is_symlink() or git(source, "rev-parse", "--show-toplevel") != str(source):
        raise Refusal("follow canonical source is not its owned checkout")
    if git(source, "symbolic-ref", "--short", "HEAD") != "main" or git(source, "status", "--porcelain"):
        raise Refusal("follow canonical main has payload; preserve it and its Auto worktrees")
    for mode in ((), ("--push",)):
        if git(source, "remote", "get-url", *mode, "--all", "origin") != upstream:
            raise Refusal("follow canonical source has another remote")
    command(["git", "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"], source, env)
    command(["git", "merge", "--ff-only", "origin/main"], source, env)


def execute(path, environ=os.environ):
    summary = event.qualified(path, environ)
    if str(summary["installation_id"]) != environ.get("WHARF_FOLLOW_INSTALLATION_ID") or not environ.get("WHARF_FOLLOW_TOKEN"):
        raise Refusal("follow token belongs to another installation or is absent")
    if not governed(summary, environ["WHARF_FOLLOW_TOKEN"]):
        return {"state": "not-governed", "repository": summary["repository"]}
    with tempfile.TemporaryDirectory(prefix="wharf-follow-") as temporary:
        root = Path(temporary).resolve()
        source = seat(root, summary)
        home = root / "home"
        home.mkdir()
        env = environment(home, root, summary["repository"], environ)
        synchronize(source, summary, env)
        command(["plumb", "configuration", "install"], source, env)
        command(["plumb", "follow", str(source), "--json"], source, env)
    return {"state": "executed", "repository": summary["repository"], "delivery": summary["delivery"]}
