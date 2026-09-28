from pathlib import Path, PurePosixPath

from lib.refusal import Conflict, Refusal


class Directory:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        relative = PurePosixPath(key)
        if not key or relative.is_absolute() or ".." in relative.parts:
            raise Refusal(f"key {key!r} is not a plain relative key")
        return self.root.joinpath(*relative.parts)

    def exists(self, key):
        return self.path(key).is_file()

    def get(self, key):
        if not self.exists(key):
            raise Refusal(f"GET {key} answered 404")
        return self.path(key).read_bytes()

    def create(self, key, body, headers=None):
        if self.exists(key):
            raise Conflict(key)
        self.put(key, body, headers)

    def put(self, key, body, headers=None):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
