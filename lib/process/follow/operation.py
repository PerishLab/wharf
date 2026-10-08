import json
import os
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from lib.content import canonical
from lib.media import node
from lib.process import git
from lib.process.follow import event
from lib.refusal import Refusal

TIMEOUT = 1800


def directory(environ):
    value = environ.get("WHARF_FOLLOW_DATA_ROOT", "")
    root = Path(value)
    if not value or not root.is_absolute() or root.resolve() != root or root == Path(root.anchor):
        raise Refusal("follow needs one explicit canonical persistent data directory")
    for name in ("RUNNER_TEMP", "GITHUB_WORKSPACE"):
        transient = environ.get(name)
        if transient and root.is_relative_to(Path(transient).resolve()):
            raise Refusal("follow data must live outside the runner's disposable directories")
    return root


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


def api(path, token):
    request = urllib.request.Request("https://api.github.com/" + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "wharf-follow"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return event.decode(response.read(event.LIMIT + 1))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Refusal("follow repository qualification was refused by GitHub") from error
    except (OSError, ValueError) as error:
        raise Refusal("follow repository qualification is unknown") from error


def governed(summary, token, reader=api):
    repository = summary["repository"]
    held = reader(f"repos/{repository}", token)
    if not isinstance(held, dict) or (held.get("id"), held.get("full_name"), held.get("default_branch")) != (summary["repository_id"], repository, "main") or held.get("archived") is not False:
        raise Refusal("follow repository identity or main differs from the signed push")
    declared = reader(f"repos/{repository}/contents/plumb.toml?ref=main", token)
    if declared is None:
        return False
    if declared.get("type") != "file" or declared.get("path") != "plumb.toml":
        raise Refusal("follow needs the tracked root Plumb declaration")
    return True


def environment(home, root, environ):
    names = ("PATH", "LANG", "LC_ALL", "CI", "TMPDIR", "TMP", "TEMP", "WHARF_DOMAIN", "SSL_CERT_FILE", "SSL_CERT_DIR", "RUNNER_TRACKING_ID")
    held = {name: environ[name] for name in names if name in environ}
    held.update({"HOME": str(home), "GH_CONFIG_DIR": str(home / "gh"), "GH_HOST": "github.com", "GH_TOKEN": environ["GH_TOKEN"], "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "PLUMB_HOME": str(root / "plumb"), "RUSTUP_HOME": environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")), "CARGO_HOME": environ.get("CARGO_HOME", str(Path.home() / ".cargo"))})
    if environ.get(node.READER):
        held[node.READER] = environ[node.READER]
    return node.reading(home, held)


def cancelled(number, frame):
    raise Refusal("follow was cancelled; preserve source and Auto recovery state")


@contextmanager
def cancellation():
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        for number in previous:
            signal.signal(number, cancelled)
        yield
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
        raise Refusal("follow termination is unknown; preserve recovery state") from error


def command(argv, cwd, env):
    print(json.dumps({"command": argv}), file=sys.stderr)
    with cancellation():
        process = None
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=sys.stderr, stderr=subprocess.STDOUT, start_new_session=True)
            if process.wait(timeout=TIMEOUT) != 0:
                raise Refusal("follow step failed; preserve source and any existing Auto recovery state")
        except subprocess.TimeoutExpired as error:
            raise Refusal("released follow exceeded its finite execution budget; preserve recovery state") from error
        finally:
            if process is not None:
                reap(process)


def synchronize(source, summary, env):
    remote = f"https://github.com/{summary['repository']}.git"
    if not source.exists():
        command(["git", "clone", "--single-branch", "--branch", "main", "--no-tags", remote, str(source)], source.parent, env)
    if source.is_symlink() or git(source, "rev-parse", "--show-toplevel") != str(source):
        raise Refusal("follow canonical source is not its owned checkout")
    if git(source, "symbolic-ref", "--short", "HEAD") != "main" or git(source, "status", "--porcelain"):
        raise Refusal("follow canonical main has payload; preserve it and its Auto worktrees")
    for mode in ((), ("--push",)):
        if git(source, "remote", "get-url", *mode, "--all", "origin") != remote:
            raise Refusal("follow canonical source has another remote")
    command(["git", "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"], source, env)
    command(["git", "merge", "--ff-only", "origin/main"], source, env)


def execute(path, environ=os.environ):
    summary = event.qualified(path, environ)
    if str(summary["installation_id"]) != environ.get("WHARF_FOLLOW_INSTALLATION_ID") or not environ.get("GH_TOKEN"):
        raise Refusal("follow token belongs to another installation or is absent")
    root = directory(environ)
    if not governed(summary, environ["GH_TOKEN"]):
        return {"state": "not-governed", "repository": summary["repository"]}
    source = seat(root, summary)
    with tempfile.TemporaryDirectory(prefix="wharf-follow-home-") as temporary:
        home = Path(temporary)
        env = environment(home, root, environ)
        command(["gh", "auth", "setup-git", "--hostname", "github.com"], home, env)
        synchronize(source, summary, env)
        command(["plumb", "follow", str(source), "--github-command", "gh", "--json"], source, env)
    return {"state": "executed", "repository": summary["repository"], "delivery": summary["delivery"]}
