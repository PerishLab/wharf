import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lib.content import preview
from lib.content.static import evidence, runtime, source
from lib.refusal import Refusal

CONFIGURATION = {
    "core.hooksPath": "/dev/null", "core.fsmonitor": "false", "core.attributesFile": "/dev/null",
    "uploadpack.packObjectsHook": "", "protocol.file.allow": "always",
}


@dataclass(frozen=True)
class Source:
    root: Path
    intent: dict
    target: dict


@dataclass
class Seat:
    root: Path
    configuration: dict
    quiescent: bool = True


def environment(home):
    held = evidence.clean({"PATH": "/usr/bin:/bin"}, home)
    held["GIT_CONFIG_COUNT"] = str(len(CONFIGURATION))
    for index, (key, value) in enumerate(CONFIGURATION.items()):
        held[f"GIT_CONFIG_KEY_{index}"] = key
        held[f"GIT_CONFIG_VALUE_{index}"] = value
    return held


def acquired(root, expected):
    preview.shape(expected, {"repository", "commit", "tree"}, "source acquisition identity")
    preview.matches(expected["repository"], preview.REPOSITORY, "source repository")
    for key in ("commit", "tree"):
        preview.matches(expected[key], preview.HEX[40], "source " + key)
    root = runtime.directory(Path(root))
    metadata = root / ".git"
    if metadata.is_symlink() or not metadata.is_dir() or (metadata / "shallow").exists():
        raise Refusal("Preview acquisition requires independent full-history Git storage")
    for entry in metadata.rglob("*"):
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise Refusal("Preview acquired Git metadata contains an unsafe entry")
    with tempfile.TemporaryDirectory(prefix="wharf-preview-readback-") as home:
        env = environment(home)
        allowed = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates", "remote.origin.url", "remote.origin.fetch", "gc.auto"}
        configuration = source.git(root, ["config", "--local", "--no-includes", "--null", "--list"], env)
        if any(entry.split("\n", 1)[0].lower() not in allowed for entry in configuration.split("\0") if entry):
            raise Refusal("Preview acquired Git configuration contains credentials or unsafe controls")
        independent(root, env)
        if source.git(root, ["rev-parse", "--show-toplevel"], env) != str(root) or source.git(root, ["status", "--porcelain", "--untracked-files=all"], env):
            raise Refusal("Preview acquired source is not an exact clean repository root")
        for key, expression in (("commit", "HEAD"), ("tree", "HEAD^{tree}")):
            if source.git(root, ["rev-parse", expression], env) != expected[key]:
                raise Refusal("Preview acquired commit/tree differs from its exact request")
        if source.git(root, ["remote", "get-url", "origin"], env) != f"https://github.com/{expected['repository']}.git":
            raise Refusal("Preview acquired origin differs from its exact repository")
        if any(line.startswith("160000 ") for line in source.git(root, ["ls-files", "--stage"], env).splitlines()):
            raise Refusal("Preview source acquisition does not admit submodules")
        source.fresh(root, env)
    return {"schema": "wharf.preview.acquisition/v1", "root": str(root), **expected}


def qualified(request, env):
    root = Path(request.root)
    if not root.is_absolute() or root.resolve() != root:
        raise Refusal("Preview staging source must be an absolute real checkout without symlink ancestors")
    config = source.qualify(root, request.intent, request.target, env)
    source.fresh(root, env)
    generated = source.output(root, config["directory"])
    if generated.exists():
        raise Refusal("Preview staging source already carries generated output")
    if source.git(root, ["check-ignore", "--", config["directory"] + "/"], env).rstrip("/") != config["directory"]:
        raise Refusal("Preview staged assets must be ignored build output")
    return config


def independent(root, env):
    runtime.directory(root)
    metadata = root / ".git"
    if metadata.is_symlink() or not metadata.is_dir():
        raise Refusal("Preview staging must own a real independent Git directory")
    common = Path(source.git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"], env))
    if common != metadata or (metadata / "objects/info/alternates").exists() or (metadata / "commondir").exists():
        raise Refusal("Preview staging must not share Git object or common storage")
    if (metadata / "hooks").exists():
        raise Refusal("Preview staging must not inherit hooks")
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file() and path.stat().st_nlink != 1:
                raise Refusal("Preview staging must not share hardlinked files")


@contextmanager
def prepare(request):
    temporary = tempfile.mkdtemp(prefix="wharf-preview-source-")
    seat = None
    try:
        parent = runtime.directory(Path(temporary))
        home, template, destination = (parent / name for name in ("home", "template", "source"))
        home.mkdir()
        template.mkdir()
        env = environment(home)
        before = qualified(request, env)
        try:
            source.git(parent, ["clone", "--no-local", "--no-checkout", "--template", str(template), "--", str(request.root), str(destination)], env)
            source.git(destination, ["remote", "remove", "origin"], env)
            source.git(destination, ["remote", "add", "origin", f"https://github.com/{request.intent['repository']}.git"], env)
            source.git(destination, ["checkout", "--detach", request.intent["source"]["commit"]], env)
            independent(destination, env)
            after = qualified(Source(destination, request.intent, request.target), env)
            if before != after or qualified(request, env) != before:
                raise Refusal("Preview source changed while its independent workspace was prepared")
            seat = Seat(destination, after)
            yield seat
        except OSError as error:
            raise Refusal("Preview disposable workspace preparation or cleanup failed") from error
    finally:
        if seat is not None and not seat.quiescent:
            raise Refusal(f"Preview workspace retained pending confirmed guest teardown: {temporary}")
        shutil.rmtree(temporary)
