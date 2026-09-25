import tomllib
from pathlib import Path


def release(source):
    path = Path(source) / "plumb.toml"
    return tomllib.loads(path.read_text()).get("release", {}) if path.is_file() else {}
