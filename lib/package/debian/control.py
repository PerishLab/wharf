import re

from lib.refusal import Refusal

FIELD = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*):(.*)")
RELATION = re.compile(r"([a-z0-9][a-z0-9+.-]+)(?::[a-z0-9-]+)?(?:\s*\([^)]*\))?(?:\s*\[[^\]]*\])?")
PLACEHOLDER = "__VERSION__"
RELATIONS = ("pre-depends", "depends")


def fields(text):
    held = []
    for line in text.splitlines():
        if not line.strip():
            raise Refusal("a Debian control template holds one paragraph and no blank lines")
        if line[0] in " \t":
            if not held:
                raise Refusal("a Debian control template starts with a field, not a continuation")
            held[-1][1].append(line)
            continue
        matched = FIELD.fullmatch(line)
        if not matched:
            raise Refusal(f"a Debian control line {line!r} is not a field")
        held.append((matched.group(1), [line]))
    names = [name.lower() for name, _ in held]
    doubled = sorted({name for name in names if names.count(name) > 1})
    if doubled:
        raise Refusal(f"a Debian control template sets {', '.join(doubled)} more than once")
    return held


def value(held, name):
    for field, lines in held:
        if field.lower() == name:
            return " ".join([lines[0].split(":", 1)[1].strip(), *(line.strip() for line in lines[1:])]).strip()
    return None


def checked(held, binary, architecture):
    if value(held, "package") != binary:
        raise Refusal(f"a Debian control template names Package {value(held, 'package')!r}; the package carries {binary!r} and is named after it")
    if value(held, "version") != PLACEHOLDER:
        raise Refusal(f"a Debian control template leaves Version to wharf as {PLACEHOLDER}")
    if value(held, "architecture") not in (None, architecture):
        raise Refusal(f"a Debian control template may only name Architecture {architecture}, which wharf writes")
    if "$" in "".join(line for _, lines in held for line in lines):
        raise Refusal("a Debian control template holds no substitution variables; nothing expands them here")
    return held


def rendered(text, binary, version, architecture):
    if text.count(PLACEHOLDER) != 1:
        raise Refusal(f"a Debian control template holds {PLACEHOLDER} exactly once")
    held = checked(fields(text), binary, architecture)
    lines = []
    for name, body in held:
        if name.lower() == "architecture":
            continue
        lines += [f"{name}: {version}", f"Architecture: {architecture}"] if name.lower() == "version" else body
    return "\n".join(lines) + "\n"


def depends(text):
    held = fields(text)
    found = []
    for name in RELATIONS:
        for relation in filter(None, (part.strip() for part in (value(held, name) or "").split(","))):
            first = relation.split("|", 1)[0].strip()
            matched = RELATION.fullmatch(first)
            if not matched:
                raise Refusal(f"a Debian relation {relation!r} is not one wharf can install")
            found.append(matched.group(1))
    return list(dict.fromkeys(found))
