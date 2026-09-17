from lib.cargo.manifest import read
from lib.refusal import Refusal


def index(source):
    entries = {}
    for entry in read(source, "Cargo.lock").get("package", []):
        entries.setdefault(entry["name"], []).append(entry)
    return entries


def pick(entries, reference):
    name, *rest = reference.split(" ")
    candidates = [entry for entry in entries.get(name, []) if not rest or entry["version"] == rest[0]]
    if len(candidates) != 1:
        raise Refusal(f"Cargo.lock cannot resolve {reference!r} unambiguously")
    return candidates[0]


def closure(source, roots):
    entries = index(source)
    seen = {}
    pending = [pick(entries, root) for root in roots]
    while pending:
        entry = pending.pop()
        identity = (entry["name"], entry["version"], entry.get("source", ""))
        if identity in seen:
            continue
        seen[identity] = entry.get("checksum", "")
        pending.extend(pick(entries, reference) for reference in entry.get("dependencies", []))
    return [[*identity, seen[identity]] for identity in sorted(seen)]
