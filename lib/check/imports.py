import ast
from pathlib import Path

RESOURCE_OWNER = "lib/resources.py"


def module(path):
    parts = list(path.with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def targets(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module
            yield from ((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)


def direction(path, tree):
    head = path.parts[0]
    findings = []
    for line, target in targets(tree):
        root = target.split(".")[0]
        if head == "lib" and root == "scripts":
            findings.append(f"{path}:{line}: lib must not import scripts ({target})")
        if head == "scripts" and root == "scripts" and target != module(path):
            findings.append(f"{path}:{line}: scripts must not import each other ({target})")
    return findings


def location(path, tree):
    if str(path) == RESOURCE_OWNER:
        return []
    names = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "__file__"]
    literals = [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.split("/")[0] == "resources" and "/" in node.value]
    return [f"{path}:{node.lineno}: only {RESOURCE_OWNER} may locate files or resources" for node in names + literals]


def graph(parsed):
    edges = {}
    for path, tree in parsed.items():
        if path.parts[0] == "lib":
            known = {module(other) for other in parsed if other.parts[0] == "lib"}
            edges[module(path)] = {target for _, target in targets(tree) if target in known and target != module(path)}
    return edges


def cycles(edges):
    findings = []
    state = {}

    def visit(node, trail):
        state[node] = "open"
        for target in sorted(edges.get(node, ())):
            if state.get(target) == "open":
                findings.append("lib import cycle: " + " -> ".join(trail[trail.index(target):] + [target]))
            elif target not in state:
                visit(target, trail + [target])
        state[node] = "closed"

    for node in sorted(edges):
        if node not in state:
            visit(node, [node])
    return findings


def check(root, paths):
    parsed = {path: ast.parse((Path(root) / path).read_text()) for path in paths if path.suffix == ".py"}
    findings = []
    for path, tree in parsed.items():
        findings += direction(path, tree) + location(path, tree)
    return findings + cycles(graph(parsed))
