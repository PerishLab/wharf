import json
from pathlib import Path, PurePosixPath

from lib.refusal import Refusal

ROOT = Path(__file__).resolve().parent.parent.parent / "resources"


def locate(name):
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or "\\" in name or ".." in relative.parts:
        raise Refusal(f"resource name {name!r} is not a plain relative name")
    return ROOT.joinpath(*relative.parts)


def read_bytes(name):
    path = locate(name)
    if not path.is_file():
        raise Refusal(f"resource {name} does not exist")
    return path.read_bytes()


def read_json(name):
    return json.loads(read_bytes(name))


def names(prefix):
    directory = locate(prefix)
    if not directory.is_dir():
        raise Refusal(f"resource directory {prefix} does not exist")
    return sorted(str(PurePosixPath(prefix) / path.name) for path in directory.iterdir() if path.is_file())
