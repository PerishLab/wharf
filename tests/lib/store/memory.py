import hashlib

from lib.refusal import Conflict


class Memory:
    def __init__(self):
        self.objects = {}
        self.headers = {}
        self.writes = []
        self.copies = []

    def head(self, key):
        return hashlib.md5(self.objects[key]).hexdigest() if key in self.objects else None

    def exists(self, key):
        return key in self.objects

    def get(self, key):
        return self.objects[key]

    def create(self, key, body, headers=None):
        if key in self.objects:
            raise Conflict(key)
        self.put(key, body, headers)

    def swap(self, key, body, etag):
        if self.head(key) != etag:
            raise Conflict(key)
        self.put(key, body, {"Content-Type": "application/json"})

    def put(self, key, body, headers=None):
        self.objects[key] = body
        self.headers[key] = dict(headers or {})
        self.writes.append(key)

    def copy(self, source, key):
        if key not in self.objects:
            self.copies.append(key)
            self.put(key, self.objects[source])

    def prefixes(self, prefix):
        found = {prefix + key[len(prefix):].split("/", 1)[0] + "/" for key in self.objects if key.startswith(prefix) and "/" in key[len(prefix):]}
        return sorted(found)
