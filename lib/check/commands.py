import ast
import re
from pathlib import Path

from lib.content import resources

LAW = resources.read_json("check/commands.json")
COMMAND = re.compile(LAW["command"])
PREPARED = set(LAW["prepared"])
RUN = re.compile(r"^(\s*)run: (.*)$")


def body(matched, rest):
    indent, value = matched.groups()
    if value != "|":
        return value
    held = []
    for line in rest:
        if line.strip() and not line.startswith(indent + " "):
            break
        held.append(line[len(indent) + 2:])
    return "\n".join(held).strip("\n")


def blocks(text):
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        matched = RUN.match(line)
        if matched:
            yield number, body(matched, lines[number:])


def spoken(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "ACTIONS" for target in node.targets):
            return {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    return set()


def entries(root, paths):
    held = [path for path in paths if path.parts[0] == "scripts" and path.suffix == ".py"]
    return {path.stem: spoken(ast.parse((Path(root) / path).read_text())) for path in held}


def refused(command, known):
    matched = COMMAND.fullmatch(command)
    if not matched:
        return f"a run block is one {LAW['shape']}, or a prepared command listed in check/commands.json"
    script, action = matched.groups()
    if script not in known:
        return f"a run block names scripts.{script}, which does not exist"
    if action not in known[script]:
        return f"a run block names the action {action}, which scripts.{script} does not take"
    return None


def check(root, paths):
    known = entries(root, paths)
    findings = []
    for path in [path for path in paths if path.suffix in (".yml", ".yaml")]:
        held = [(number, refused(command, known)) for number, command in blocks((Path(root) / path).read_text()) if command not in PREPARED]
        findings += [f"{path}:{number}: {finding}" for number, finding in held if finding]
    return findings
