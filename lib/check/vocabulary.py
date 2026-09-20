import ast
import re
from pathlib import Path

from lib.content import resources

VOCABULARY = resources.read_json("vocabulary.json")
CLAIMED = VOCABULARY["claimed"]
EXTERNAL = set(VOCABULARY["external"])


def words(text):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {word.lower() for word in re.split(r"[^A-Za-z0-9]+", spaced) if word}


def spoken(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def check(root, paths):
    findings = [f"{path}: file name uses claimed word {word!r}" for path in paths for word in words(str(path)) & set(CLAIMED)]
    for path in [path for path in paths if path.suffix == ".py" and path.parts[0] != "tests"]:
        tree = ast.parse((Path(root) / path).read_text())
        for line, text in [(line, text) for line, text in spoken(tree) if text not in EXTERNAL]:
            findings += [f"{path}:{line}: uses claimed word {word!r} ({CLAIMED[word]})" for word in sorted(words(text) & set(CLAIMED))]
    return findings
