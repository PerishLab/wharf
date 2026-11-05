import hashlib
import json
import re

from lib.content import resources
from lib.content.static import evidence
from lib.refusal import Refusal

HEX = re.compile(r"[0-9a-f]{40}")
RELEASES = resources.read_json("releases.json")

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


def object_id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise Refusal("release snapshot needs an exact Git object identity")
    return value


def stable(controls, reader):
    observed = {}
    for name in ("plumb", "ectropy"):
        authority = RELEASES["authority"].format(name=name)
        try:
            pointer = evidence.decode(reader(f"{authority}/v1/channels/stable.json"))
            if not isinstance(pointer, dict) or pointer.get("schema") != 1 or pointer.get("product") != name or pointer.get("channel") != "stable":
                raise Refusal("release snapshot has no current stable control pointer")
            if pointer.get("releaseVersion") != controls[name].removeprefix(name + " "):
                raise Refusal(f"latest stable {name} changed; prepare current tools and a fresh plan")
            object_id(pointer.get("commit"))
            location = f"{authority}/v1/releases/stable/{pointer['releaseVersion']}/seal.json"
            sealed = pointer.get("seal", {})
            if not isinstance(sealed, dict) or sealed.get("url") != location:
                raise Refusal("release snapshot stable seal is not canonical")
            body = reader(location)
            if len(body) != sealed.get("size") or hashlib.sha256(body).hexdigest() != sealed.get("sha256"):
                raise Refusal("release snapshot stable seal differs from its pointer")
            seal = evidence.decode(body)
            if not isinstance(seal, dict) or any(seal.get(field) != pointer[field] for field in ("product", "releaseVersion", "commit")):
                raise Refusal("release snapshot stable seal identity disagrees")
            if name == "plumb" and seal.get("guard") != controls["authority"]:
                raise Refusal("running Plumb differs from its current stable seal authority")
            observed[name] = {field: pointer[field] for field in ("releaseVersion", "commit")}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Refusal("latest stable control authority is unreadable") from error
    return observed



def qualified(context, head, result, body):
    identity = context["identity"]
    document = evidence.decode(body)
    commit = document.get("commit") if isinstance(document, dict) else None
    rooted = commit == head
    source = {"commit": head, "tree": result["tree"]} if rooted else {"commit": guard(commit, object_id), "tree": identity["tree"]}
    intent = {"repository": identity["repository"], "source": source}
    proof = evidence.guard(body, intent, context["plumb"], rooted)
    if {"producer": proof["guard"]["plumb"], "depot": proof["guard"]["depot"]} != context["authority"]:
        raise Refusal("release snapshot Guard differs from its running authority")
    resolved = proof["guard"].get("resolution")
    if resolved != {"context": "ci-latest", "tree": result["tree"], "packages": result["packages"]}:
        raise Refusal(resolution(resolved, {"context": "ci-latest", "tree": result["tree"], "packages": result["packages"]}))
    return proof
