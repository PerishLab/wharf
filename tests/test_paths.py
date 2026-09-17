import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "wharf"
PATHS = {"ship", "depot"}


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


class Paths(unittest.TestCase):
    def test_distribution_paths_do_not_import_each_other(self):
        for file in PACKAGE.rglob("*.py"):
            parts = file.relative_to(PACKAGE).parts
            owner = parts[0] if parts[0] in PATHS else None
            for module in imports(file):
                segments = module.split(".")
                if segments[0] != "wharf" or len(segments) < 2 or segments[1] not in PATHS:
                    continue
                with self.subTest(file=str(file), module=module):
                    self.assertTrue(owner is not None and segments[1] == owner or parts == ("__main__.py",))
