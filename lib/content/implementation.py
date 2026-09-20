import ast
import hashlib
import importlib.util
from pathlib import Path

from lib.content import resources


def package():
    return Path(importlib.util.find_spec("lib").origin).parent


def source(module):
    parts = module.split(".")
    if parts[0] != "lib" or len(parts) < 2:
        return None
    candidate = package().joinpath(*parts[1:])
    for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if path.is_file():
            return path
    return None


def imported(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield from [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def closure(modules):
    seen = {}
    pending = list(modules)
    while pending:
        module = pending.pop()
        path = source(module)
        if module in seen or path is None:
            continue
        seen[module] = path
        pending.extend(imported(path))
    return seen


def digest(*modules):
    files = closure(modules)
    return {module: hashlib.sha256(path.read_bytes()).hexdigest() for module, path in sorted(files.items())}


def resourced(modules, names):
    held = digest(*modules)
    held.update({f"resource:{name}": hashlib.sha256(resources.read_bytes(name)).hexdigest() for name in names})
    return held
