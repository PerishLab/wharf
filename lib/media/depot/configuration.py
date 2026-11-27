import datetime
from pathlib import Path

from lib.content import consigned, marker
from lib.content.static import evidence
from lib.media import depot, edit, lineage
from lib.refusal import Refusal
from lib.store import r2

REPOSITORY = "PerishLab/plumb"
RULES = "crates/cli/rules"
KIND = "configuration"
BASE = depot.LAYOUT["source"].format(name="plumb")
CHANNELS = frozenset({"alpha", "beta", "rc", "stable"})
PINNED = frozenset({"channel", "version", "generation", "manifest"})
REFERENCE = frozenset({"url", "sha256", "size"})


def carried(repository, source):
    return repository == REPOSITORY and (Path(source) / RULES).is_dir()


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def descriptor(pointer):
    return {
        "channel": pointer["channel"],
        "version": pointer["version"],
        "generation": pointer["generation"],
        "manifest": dict(pointer["manifest"]),
    }


def read(reader, url):
    try:
        body = reader(url)
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refusal("depot configuration authority is unreadable") from error
    if body is None or not isinstance(body, bytes) or len(body) > evidence.LIMIT:
        raise Refusal("depot configuration response is missing or exceeds bounds")
    return body


def checked_descriptor(pinned):
    if not isinstance(pinned, dict) or frozenset(pinned) != PINNED:
        raise Refusal("validation configuration descriptor is malformed")
    channel, version, generation, ref = pinned["channel"], pinned["version"], pinned["generation"], pinned["manifest"]
    if channel not in CHANNELS or not isinstance(version, str) or not marker.holds(version) or marker.channel(version) != channel:
        raise Refusal("validation configuration channel or version is invalid")
    if not isinstance(generation, str) or not depot.DIGEST.fullmatch(generation):
        raise Refusal("validation configuration generation is invalid")
    if not isinstance(ref, dict) or frozenset(ref) != REFERENCE:
        raise Refusal("validation configuration manifest reference is malformed")
    expected = f"{BASE}/{depot.route(channel, version, KIND)}/generations/{generation}/manifest.json"
    if ref["url"] != expected or not isinstance(ref["sha256"], str) or not depot.DIGEST.fullmatch(ref["sha256"]):
        raise Refusal("validation configuration manifest url or sha256 is invalid")
    if type(ref["size"]) is not int or not (0 < ref["size"] <= evidence.LIMIT):
        raise Refusal("validation configuration manifest size is invalid")
    return {"channel": channel, "version": version, "generation": generation, "manifest": dict(ref)}


def bundled(source):
    rules = Path(source) / RULES
    consigned.configuration(rules)
    return {
        f"rules/{relative}": (dict(entry, path=f"rules/{relative}"), body)
        for relative, (entry, body) in edit.directory(rules).items()
    }


def objects_match(source, objects):
    expected = sorted((entry for entry, _ in bundled(source).values()), key=lambda item: item["path"])
    if objects != expected:
        raise Refusal("staged depot configuration objects differ from HEAD crates/cli/rules")


def read_manifest(source, pinned, reader):
    checked = checked_descriptor(pinned)
    ref = checked["manifest"]
    body = read(reader, ref["url"])
    if len(body) != ref["size"] or depot.sha(body) != ref["sha256"]:
        raise Refusal("validation configuration manifest bytes differ from reference")
    document = evidence.decode(body)
    if not isinstance(document, dict) or document.get("format") != depot.FORMAT or document.get("product") != "plumb":
        raise Refusal("validation configuration manifest identity is invalid")
    if document.get("channel") != checked["channel"] or document.get("version") != checked["version"] or document.get("kind") != KIND:
        raise Refusal("validation configuration manifest projection disagrees")
    if depot.sha(depot.compact(document)) != checked["generation"]:
        raise Refusal("validation configuration manifest generation digest disagrees")
    objects_match(source, document.get("objects"))
    return checked, document


def authenticate(source, channel, reader=depot.fetch):
    pointer = evidence.decode(read(reader, f"{BASE}/channels/{channel}/configurations/latest.json"))
    if not isinstance(pointer, dict) or pointer.get("format") != depot.FORMAT or pointer.get("product") != "plumb":
        raise Refusal("staged configuration channel pointer is malformed")
    if pointer.get("channel") != channel or pointer.get("kind") != KIND:
        raise Refusal("staged configuration channel pointer names another projection")
    checked, document = read_manifest(source, descriptor(pointer), reader)
    if depot.pointer(document, BASE, pointer.get("previousGeneration"), pointer.get("createdAt")) != pointer:
        raise Refusal("staged configuration channel pointer does not bind its manifest")
    return checked


def lodge(source, identity, bucket=None):
    content = bundled(source)
    held = depot.Release(identity["repository"], identity["marker"], identity["commit"], identity["tree"])
    channel = depot.channel_of(held.marker)
    store = bucket if bucket is not None else r2.writer(depot.LAYOUT["bucket"].format(name="plumb"), "DEPOT")
    base = lineage.parent(store, (channel, held.marker, KIND))
    result = edit.stage(store, edit.Change(held, KIND, content, base, now()))
    discovery = lineage.discovery(store, held, KIND, result["generation"])
    pointer, _ = lineage.pointer(store, (channel, held.marker, KIND))
    return descriptor(pointer), {"generation": result["generation"], "state": result["state"], "discovery": discovery}


def prepare(scoped, reader=depot.fetch, bucket=None):
    if not carried(scoped["repository"], scoped["source"]):
        return None
    if marker.channel(scoped["marker"]) == "stable":
        return authenticate(scoped["source"], "rc", reader)
    pinned, _ = lodge(scoped["source"], scoped, bucket)
    return pinned


def verify_planned(source, repository, pinned, reader=depot.fetch):
    if pinned is None:
        if carried(repository, source):
            raise Refusal("plumb binary validation requires a pinned depot configuration")
        return None
    checked, _ = read_manifest(source, pinned, reader)
    return checked


def basis(primary, pinned):
    entry = {"kind": "binary-validate", "binary": primary}
    if pinned is not None:
        entry["configuration"] = pinned["generation"]
    return {"entry": entry}


def release(held, pinned):
    data = {"repository": held["repository"], "marker": held["marker"]}
    if pinned is not None:
        data["configuration"] = pinned
    return data


def promote(source, context, reader=depot.fetch, bucket=None):
    if not carried(context["repository"], source) or marker.channel(context["marker"]) != "stable":
        return None
    authenticate(source, "rc", reader)
    _, summary = lodge(source, context, bucket)
    return summary
