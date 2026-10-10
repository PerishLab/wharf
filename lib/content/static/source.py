import re
import datetime
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from lib.content import canonical, declaration
from lib.content.lane import preview
from lib.content.static import declaration as lane, evidence
from lib.refusal import Refusal

WORKER = {"name", "account_id", "compatibility_date", "workers_dev", "assets", "previews"}


def git(root, arguments, env):
    try:
        done = subprocess.run(["git", "-C", str(root), *arguments], env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal("static source Git observation failed") from error
    if done.returncode != 0:
        raise Refusal("static source Git observation refused")
    return done.stdout.strip()


def file(root, name, tracked):
    path = root / name
    if name not in tracked or path.is_symlink() or not path.is_file():
        raise Refusal(f"static source {name} must be a tracked regular file")
    cursor = root
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise Refusal(f"static source {name} traverses a symlink")
    return path.read_bytes()


def worker(body):
    text = re.sub(r"^\s*//.*$", "", body.decode(), flags=re.M)
    held = evidence.decode(text)
    preview.shape(held, WORKER, "static worker")
    if held["workers_dev"] is not False or held["previews"] != {}:
        raise Refusal("static worker must disable production and declare empty previews")
    preview.matches(held["account_id"], preview.HEX[32], "worker account")
    lane.resource(held["name"])
    if not isinstance(held["compatibility_date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", held["compatibility_date"]):
        raise Refusal("static worker needs an explicit compatibility date")
    try:
        datetime.date.fromisoformat(held["compatibility_date"])
    except ValueError as error:
        raise Refusal("static worker compatibility date is not a calendar date") from error
    preview.shape(held["assets"], {"directory", "not_found_handling"}, "static assets")
    if held["assets"]["not_found_handling"] != "404-page":
        raise Refusal("static assets must use 404-page handling")
    directory(held["assets"]["directory"])
    return held


def directory(value):
    if not isinstance(value, str):
        raise Refusal("static assets directory must be text")
    value = value.removeprefix("./")
    path = PurePosixPath(value)
    if not value or value == "." or path.is_absolute() or str(path) != value or "\\" in value or any(part.startswith(".") for part in path.parts):
        raise Refusal("static assets directory must be a normalized private build directory")
    return value


def configuration(root, app, tracked):
    root = Path(root).resolve()
    try:
        document = tomllib.loads(file(root, "plumb.toml", tracked).decode())
    except (ValueError, UnicodeError) as error:
        raise Refusal("static source declaration is invalid") from error
    paths = declaration.lanes(root)
    declared = document.get("lane", {}).get("app", {}).get(app)
    if not isinstance(declared, dict):
        raise Refusal("static app is not explicitly declared")
    path = declared["path"]
    if paths.get(path) != declared:
        raise Refusal("static app declaration disagrees with its qualified path")
    config = worker(file(root, f"{path}/wrangler.jsonc", tracked))
    if config["account_id"] != declared["mapping"]["account"] or config["name"] != declared["mapping"]["resource"]:
        raise Refusal("static Worker differs from its explicit lane mapping")
    package = evidence.decode(file(root, f"{path}/package.json", tracked))
    if isinstance(package, dict) and any(key in package for key in ("engines", "packageManager")):
        raise Refusal("static packages must not declare tool versions; Plumb carries the domain")
    if not isinstance(package, dict) or package.get("private") is not True or package.get("name") != declared["package"] or not isinstance(package.get("scripts"), dict) or not isinstance(package["scripts"].get("build"), str) or not package["scripts"]["build"].strip():
        raise Refusal("static app needs a matching private package with a build script")
    if any(package["scripts"].get(key) for key in ("prebuild", "postbuild")):
        raise Refusal("static app must not add implicit prebuild/postbuild scripts")
    if not re.fullmatch(r"(?:@[a-z0-9-]+/)?[a-z0-9][a-z0-9._-]*", package["name"]):
        raise Refusal("static package must have a literal npm name, not a filter selector")
    for name in (name for name in tracked if name.endswith("package.json") and name != f"{path}/package.json"):
        other = evidence.decode(file(root, name, tracked))
        if isinstance(other, dict) and any(key in other for key in ("engines", "packageManager")):
            raise Refusal("static packages must not declare tool versions; Plumb carries the domain")
        if isinstance(other, dict) and other.get("name") == package["name"]:
            raise Refusal("static package name must not select another workspace package")
    manifest = evidence.decode(file(root, "package.json", tracked))
    if not isinstance(manifest, dict):
        raise Refusal("static source requires a root package manifest")
    for name in ("pnpm-lock.yaml", "pnpm-workspace.yaml"):
        file(root, name, tracked)
    duplicate(root, path, config, tracked)
    npmrc(root, tracked)
    bound = {"app": app, "declaration": declared, "worker": config, "package": package}
    return dict(bound, digest=canonical.digest(bound), directory=f"{path}/{directory(config['assets']['directory'])}")


def npmrc(root, tracked):
    for name in (name for name in tracked if name.endswith(".npmrc")):
        lines = file(root, name, tracked).decode().splitlines()
        allowed = "@perishlab:registry=https://npm.pkg.github.com/"
        if any(line.strip() not in ("", allowed) for line in lines):
            raise Refusal("static install only accepts the declared read-only package registry map")


def duplicate(root, path, config, tracked):
    for name in (name for name in tracked if name.endswith("/wrangler.jsonc") and name != f"{path}/wrangler.jsonc"):
        text = re.sub(r"^\s*//.*$", "", file(root, name, tracked).decode(), flags=re.M)
        other = evidence.decode(text)
        if not isinstance(other, dict) or (other.get("account_id"), other.get("name")) == (config["account_id"], config["name"]):
            raise Refusal("static Preview target must not alias another Worker configuration")


def qualify(root, intent, target, env):
    preview.admitted(intent, target)
    if intent["operation"] != "apply":
        raise Refusal("static source qualification only accepts apply")
    root = Path(root).resolve()
    if git(root, ["rev-parse", "--show-toplevel"], env) != str(root) or git(root, ["status", "--porcelain", "--untracked-files=all"], env):
        raise Refusal("static build requires an exact clean repository root")
    if git(root, ["rev-parse", "HEAD"], env) != intent["source"]["commit"] or git(root, ["rev-parse", "HEAD^{tree}"], env) != intent["source"]["tree"]:
        raise Refusal("static source differs from the requested exact commit/tree")
    remote = git(root, ["remote", "get-url", "origin"], env)
    if remote not in (f"https://github.com/{intent['repository']}.git", f"https://github.com/{intent['repository']}", f"git@github.com:{intent['repository']}.git"):
        raise Refusal("static source origin differs from the requested GitHub repository")
    entries = git(root, ["ls-files", "--stage"], env).splitlines()
    if any(line.startswith("160000 ") for line in entries):
        raise Refusal("static build does not admit submodule source")
    tracked = set(git(root, ["ls-files"], env).splitlines())
    config = configuration(root, intent["app"], tracked)
    if config["digest"] != intent["source"]["declaration"] or config["worker"]["account_id"] != target["account"] or config["worker"]["name"] != target["worker"]:
        raise Refusal("static declaration or Worker identity differs from its exact request/registration")
    return config


def output(root, name):
    cursor = Path(root)
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise Refusal("static output traverses a symlink")
    return cursor


def fresh(root, env):
    if git(root, ["ls-files", "--others", "--ignored", "--exclude-standard"], env):
        raise Refusal("static build requires a fresh checkout without ignored payload")
