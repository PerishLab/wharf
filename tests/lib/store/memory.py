from lib.refusal import Conflict


class Memory:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def exists(self, key):
        return key in self.objects

    def get(self, key):
        return self.objects[key]

    def create(self, key, body):
        if key in self.objects:
            raise Conflict(key)
        self.objects[key] = body
        self.writes.append(key)
