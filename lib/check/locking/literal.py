import json
import re

from lib.refusal import Refusal

LIMIT = 2 * 1024 * 1024
NODES = 20000
DEPTH = 12


def refuse():
    raise Refusal("Preview lockfile is outside the supported literal subset")


def divided(text, separator):
    result, start, nesting, quote = [], 0, 0, ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            nesting += 1
        elif char in "]}":
            nesting -= 1
        elif char == separator and nesting == 0:
            if separator != ":" or text[index + 1:index + 2] in {"", " "}:
                result.append(text[start:index].strip())
                start = index + 1
        if nesting < 0 or nesting > DEPTH:
            refuse()
    if quote or nesting:
        refuse()
    result.append(text[start:].strip())
    return result


class Reader:
    def __init__(self, body):
        if not isinstance(body, bytes) or len(body) > LIMIT:
            refuse()
        try:
            text = body.decode("ascii")
        except UnicodeDecodeError:
            refuse()
        if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in text) or "\\" in text:
            refuse()
        self.lines = []
        for line in text.split("\n"):
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent % 2 or indent > DEPTH * 2 or line.endswith(" "):
                refuse()
            self.lines.append((indent, line[indent:]))
        self.cursor = 0
        self.nodes = 0

    def value(self, text, depth=0):
        self.nodes += 1
        if depth > DEPTH or self.nodes > NODES or not text or len(text) > 4096:
            refuse()
        if text.startswith("{") and text.endswith("}"):
            result = {}
            for entry in divided(text[1:-1], ",") if text != "{}" else []:
                key, value = self.entry(entry)
                self.insert(result, key, self.value(value, depth + 1))
            return result
        if text.startswith("[") and text.endswith("]"):
            return [self.value(item, depth + 1) for item in divided(text[1:-1], ",")] if text != "[]" else []
        if text.startswith("'"):
            if len(text) < 2 or not text.endswith("'") or "'" in text[1:-1]:
                refuse()
            return text[1:-1]
        if text.startswith('"'):
            try:
                value = json.loads(text)
            except ValueError:
                refuse()
            if not isinstance(value, str):
                refuse()
            return value
        if text in {"true", "false"}:
            return text == "true"
        if re.fullmatch(r"0|[1-9][0-9]*", text):
            value = int(text)
            if value > 9007199254740991:
                refuse()
            return value
        if re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            refuse()
        if text in {"null", "Null", "NULL", "~", "True", "False", "TRUE", "FALSE"}:
            refuse()
        if re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|[+-]?\.[A-Za-z0-9]+", text):
            refuse()
        if text[0].isdigit() and not re.match(r"[0-9]+\.[0-9]+\.[0-9]+(?:$|[-(])", text):
            refuse()
        if text[0] in "!&*|>@%[]{}?:,+-" or any(char in text for char in "#\"'[]{}"):
            refuse()
        return text

    def entry(self, text):
        parts = divided(text, ":")
        if len(parts) != 2:
            refuse()
        key = self.value(parts[0])
        if not isinstance(key, str) or not key or key == "<<":
            refuse()
        return key, parts[1]

    def insert(self, target, key, value):
        if key in target:
            refuse()
        target[key] = value

    def block(self, indent, depth=0):
        self.nodes += 1
        if depth > DEPTH or self.nodes > NODES or self.cursor == len(self.lines):
            refuse()
        sequence = self.lines[self.cursor][1].startswith("- ")
        result = [] if sequence else {}
        while self.cursor < len(self.lines):
            level, text = self.lines[self.cursor]
            if level < indent:
                break
            if level != indent or sequence != text.startswith("- "):
                refuse()
            self.cursor += 1
            if sequence:
                result.append(self.value(text[2:], depth + 1))
            else:
                key, value = self.entry(text)
                parsed = self.value(value, depth + 1) if value else self.block(indent + 2, depth + 1)
                self.insert(result, key, parsed)
        return result


def parse(body):
    reader = Reader(body)
    result = reader.block(0)
    if not isinstance(result, dict) or reader.cursor != len(reader.lines):
        refuse()
    return result
