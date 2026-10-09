import tomllib
from pathlib import Path, PurePosixPath

from lib.content.static import declaration
from lib.refusal import Refusal


def release(source):
    path = Path(source) / "plumb.toml"
    return tomllib.loads(path.read_text()).get("release", {}) if path.is_file() else {}


def lane_path(source, value):
    if not isinstance(value, str) or not value or "\\" in value or (len(value) > 1 and value[1] == ":"):
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


def lanes(source):
    path = Path(source) / "plumb.toml"
    try:
        if path.is_symlink():
            raise Refusal("lane declaration must not be a symlink")
        if path.exists() and not path.is_file():
            raise Refusal("lane declaration must be a regular file")
        document = tomllib.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError, UnicodeError) as error:
        raise Refusal("invalid lane declaration") from error
    apps = declaration.read(document)
    found = {}
    packages = set()
    for app in apps.values():
        directory = lane_path(source, app["path"])
        if directory in found or app["package"] in packages:
            raise Refusal("lane apps must not duplicate source paths or package selectors")
        packages.add(app["package"])
        found[directory] = app
    return found
