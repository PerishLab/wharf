import ast
import io
import re
import tokenize
from pathlib import Path

from lib.check.structure import RULES

NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def comments(path, text):
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return [f"{path}:{token.start[0]}: comments are not allowed" for token in tokens if token.type == tokenize.COMMENT]


def docstrings(path, tree):
    holders = [tree] + [node for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    return [f"{path}:{getattr(node, 'lineno', 1)}: docstrings are not allowed" for node in holders if ast.get_docstring(node, clean=False) is not None]


def parameters(path, tree):
    limit = RULES["limits"]["parameters"]
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS):
            held = node.args
            count = len(held.posonlyargs) + len(held.args) + len(held.kwonlyargs) + bool(held.vararg) + bool(held.kwarg)
            if count > limit:
                findings.append(f"{path}:{node.lineno}: {count} parameters, more than {limit}")
    return findings


def chained(node, child):
    return isinstance(node, ast.If) and node.orelse == [child] and isinstance(child, ast.If)


def deepest(node, level=0):
    inner = level + isinstance(node, NESTING)
    children = [child for child in ast.iter_child_nodes(node) if not isinstance(child, FUNCTIONS)]
    return max([inner] + [deepest(child, level if chained(node, child) else inner) for child in children])


def nesting(path, tree):
    limit = RULES["limits"]["nesting"]
    functions = [node for node in ast.walk(tree) if isinstance(node, FUNCTIONS)]
    return [f"{path}:{node.lineno}: blocks nest {deepest(node)} deep, more than {limit}" for node in functions if deepest(node) > limit]


def python(root, path):
    text = (Path(root) / path).read_text()
    tree = ast.parse(text)
    findings = comments(path, text) + docstrings(path, tree) + parameters(path, tree) + nesting(path, tree)
    lines = len(text.splitlines())
    if lines > RULES["limits"]["lines"]:
        findings.append(f"{path}: {lines} lines, more than {RULES['limits']['lines']}")
    return findings


def yaml(root, path):
    lines = (Path(root) / path).read_text().splitlines()
    return [f"{path}:{number}: comments are not allowed" for number, line in enumerate(lines, 1) if re.search(r"(^|\s)#(\s|$)", line)]


def check(root, paths):
    findings = []
    for path in paths:
        if path.suffix == ".py":
            findings += python(root, path)
        elif path.suffix in (".yml", ".yaml"):
            findings += yaml(root, path)
    return findings
