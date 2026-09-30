import tomllib
from pathlib import Path, PurePosixPath

from lib.refusal import Refusal


def release(source):
    path = Path(source) / "plumb.toml"
    return tomllib.loads(path.read_text()).get("release", {}) if path.is_file() else {}


def preview_path(source, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise Refusal("preview app path must be a relative POSIX directory")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts) or value == ".":
        raise Refusal(f"preview app path {value!r} must be a normalized relative directory")
    root = Path(source).resolve()
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise Refusal(f"preview app path {value!r} traverses a symlink")
    if not target.is_dir() or not target.resolve().is_relative_to(root):
        raise Refusal(f"preview app path {value!r} must resolve inside the product")
    return value


def previews(source):
    path = Path(source) / "plumb.toml"
    try:
        declared = tomllib.loads(path.read_text()).get("preview", {}) if path.is_file() else {}
    except tomllib.TOMLDecodeError as error:
        raise Refusal(f"invalid preview declaration: {error}") from error
    if not isinstance(declared, dict) or set(declared) - {"app"}:
        raise Refusal("preview must contain only an app table")
    apps = declared.get("app", {})
    if not isinstance(apps, dict):
        raise Refusal("preview.app must be a table")
    found = {}
    for name, app in apps.items():
        if not isinstance(app, dict) or set(app) != {"path", "package", "provider", "access"}:
            raise Refusal(f"preview app {name} must declare path, package, provider and access")
        if app["provider"] != "cfworker" or app["access"] != "public" or not isinstance(app["package"], str) or not app["package"]:
            raise Refusal(f"preview app {name} requires cfworker, explicit public access and a package name")
        directory = preview_path(source, app["path"])
        if directory in found:
            raise Refusal(f"preview app path {directory} is declared twice")
        found[directory] = app
    return found
