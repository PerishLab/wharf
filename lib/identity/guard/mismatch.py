from lib.refusal import Refusal

FIELDS = ("schema", "source", "head", "tree", "packages", "controls", "domain")
GROUPS = {
    "source": ("repository", "marker", "commit", "tree"),
    "controls": ("plumb", "ectropy", "authority", "stable"),
    "domain": ("node.version", "pnpm.version", "rust.version"),
}


def fields(expected, observed, names):
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return ["structure"]
    absent = object()
    changed = [name for name in names if expected.get(name, absent) != observed.get(name, absent)]
    if (set(expected) | set(observed)) - set(names):
        changed.append("structure")
    return changed


def describe(expected, observed):
    if not isinstance(expected, dict):
        return "plan structure"
    changed = []
    held = {name: value for name, value in expected.items() if name != "guard"}
    for name in fields(held, observed, FIELDS):
        if name in GROUPS:
            changed.extend(name + "." + key for key in fields(held.get(name), observed.get(name), GROUPS[name]))
        else:
            changed.append(name)
    if not isinstance(expected.get("guard"), dict):
        changed.append("guard missing or malformed")
    return ", ".join(changed)


def check(expected, observed):
    if not isinstance(expected, dict) or {key: value for key, value in expected.items() if key != "guard"} != observed or not isinstance(expected.get("guard"), dict):
        raise Refusal("release snapshot differs from the verified plan (" + describe(expected, observed) + "); prepare a fresh plan")


def guard(value, check):
    try:
        return check(value)
    except Refusal:
        raise Refusal("release snapshot Guard commit is missing or malformed; prepare a fresh plan") from None


def resolution(expected, observed):
    changed = ", ".join(fields(expected, observed, ("context", "tree", "packages")))
    return "release snapshot Guard resolution differs (" + changed + "); prepare and verify a fresh plan"
