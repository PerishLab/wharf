from pathlib import Path

from lib.content import resources

RULES = resources.read_json("check/structure.json")


def top(paths):
    allowed = set(RULES["top"])
    return [f"{path}: top-level entry {path.parts[0]} is not allowed" for path in paths if path.parts[0] not in allowed]


def identity(root, paths):
    present = {str(path) for path in paths}
    findings = [f"{name}: required file is missing" for name in RULES["unstructured"] if name not in present]
    for name, content in RULES["pointer"].items():
        if name not in present or (Path(root) / name).read_text() != content:
            findings.append(f"{name}: must contain exactly {content!r}")
    prose = set(RULES["prose"])
    allowed = set(RULES["unstructured"]) | set(RULES["pointer"])
    findings += [f"{path}: only {', '.join(sorted(allowed))} may be unstructured" for path in paths if path.suffix.lower() in prose and str(path) not in allowed]
    return findings


def placement(path):
    head = path.parts[0]
    if head in RULES["python"] and path.suffix != ".py":
        return f"{path}: only Python modules belong under {head}/"
    if head == ".githooks" and str(path) not in RULES["hooks"]:
        return f"{path}: only {', '.join(RULES['hooks'])} belongs under .githooks/"
    if head == ".github" and (str(path.parent) != RULES["workflows"] or path.suffix != ".yml"):
        return f"{path}: only workflow .yml files belong under {RULES['workflows']}/"
    return None


def depth(paths):
    limit = RULES["limits"]["depth"]
    return [f"{path}: nested deeper than {limit} below {path.parts[0]}/" for path in paths if len(path.parts) - 1 > limit]


def entries(paths):
    limit = RULES["limits"]["entries"]
    below = {}
    for path in paths:
        if path.name == "__init__.py":
            continue
        for level in range(1, len(path.parts)):
            below.setdefault("/".join(path.parts[:level]), set()).add(path.parts[level])
    return [f"{parent}: holds {len(names)} entries, more than {limit}" for parent, names in sorted(below.items()) if len(names) > limit]


def check(root, paths):
    placed = [finding for finding in map(placement, paths) if finding]
    return top(paths) + identity(root, paths) + placed + depth(paths) + entries(paths)
