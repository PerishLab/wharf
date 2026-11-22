from lib.cargo.manifest import read
from lib.refusal import Refusal


def index(source):
    entries = {}
    for entry in read(source, "Cargo.lock").get("package", []):
        entries.setdefault(entry["name"], []).append(entry)
    return entries


def unique(candidates, reference):
    if len(candidates) != 1:
        raise Refusal(f"Cargo.lock cannot resolve {reference!r} unambiguously")
    return candidates[0]


def workspace(entries, name):
    return unique([entry for entry in entries.get(name, []) if "source" not in entry], name)


def locator(entry):
    source = entry.get("source", "")
    if source.startswith("git+"):
        return source.rsplit("#", 1)[0]
    return source


def pick(entries, reference):
    name, *qualifiers = reference.split(maxsplit=2)
    candidates = entries.get(name, [])
    if qualifiers:
        candidates = [entry for entry in candidates if entry["version"] == qualifiers[0]]
    if len(qualifiers) == 2:
        candidates = [entry for entry in candidates if f"({locator(entry)})" == qualifiers[1]]
    return unique(candidates, reference)


def closure(source, roots):
    entries = index(source)
    seen = {}
    pending = [workspace(entries, root) for root in roots]
    while pending:
        entry = pending.pop()
        identity = (entry["name"], entry["version"], entry.get("source", ""))
        if identity in seen:
            continue
        seen[identity] = entry.get("checksum", "")
        pending.extend(pick(entries, reference) for reference in entry.get("dependencies", []))
    return [[*identity, seen[identity]] for identity in sorted(seen)]
