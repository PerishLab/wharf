import ast
from pathlib import Path

from lib.content import resources

DECLARED = resources.read_json("parameters.json")


def sequences(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            if all(isinstance(item, (ast.Starred, ast.Constant)) for item in node.elts):
                yield [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]


def taken(root, paths):
    held = set()
    for path in [path for path in paths if path.parts[0] == "scripts" and path.suffix == ".py"]:
        for names in sequences(ast.parse((Path(root) / path).read_text())):
            held.update(names)
    return held


def check(root, paths):
    held = taken(root, paths)
    unused = sorted(name for name in DECLARED["types"] if name not in held)
    findings = [f"parameters declare {name}, which no action takes" for name in unused]
    for field in ("shapes", "defaults"):
        findings += [
            f"parameters give a {field[:-1]} for {name}, which is not declared"
            for name in sorted(DECLARED[field])
            if name not in DECLARED["types"]
        ]
    return findings
