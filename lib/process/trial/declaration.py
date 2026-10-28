import hashlib
import re
import tomllib
from pathlib import Path

from lib.refusal import Refusal

SCHEMA = "wharf.trial.declaration/v1"
CHANNELS = {"stable", "alpha", "beta", "rc"}
NAME = re.compile(r"[a-z0-9_-]+")


def checked(condition, reason):
    if not condition:
        raise Refusal(reason)


def shape(value, fields, subject):
    checked(isinstance(value, dict) and set(value) == set(fields), f"{subject} fields differ")


def authority(value):
    checked(isinstance(value, str) and value.startswith("https://") and len(value) > 8 and not value.endswith("/") and not any(character.isspace() or character in "?#@" for character in value), "Seatrial requires a normalized public HTTPS authority")


def validate(value):
    shape(value, ("scenes", "cases", "image"), "guard.seatrial")
    scenes = value["scenes"]
    shape(scenes, ("authority", "product", "channel"), "Seatrial scenes")
    authority(scenes["authority"])
    checked(isinstance(scenes["product"], str) and NAME.fullmatch(scenes["product"]), "invalid Seatrial scene product")
    checked(isinstance(scenes["channel"], str) and scenes["channel"] in CHANNELS, "Seatrial scenes follow channels, never version pins")
    cases = value["cases"]
    checked(isinstance(cases, list) and cases and all(isinstance(case, str) and NAME.fullmatch(case) for case in cases), "Seatrial cases must be explicit nonempty names")
    checked(len(set(cases)) == len(cases), "Seatrial cases must be unique")
    image = value["image"]
    checked(isinstance(image, str) and image and not any(character.isspace() for character in image), "invalid Seatrial runtime image")
    if image.startswith("ghcr.io/perishlab/"):
        tag = image.rsplit("/", 1)[-1]
        checked("@" not in image and (":" not in tag or tag.split(":", 1)[1] in CHANNELS), "first-party Seatrial images follow channels or the resolved product release")
    else:
        checked(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image), "third-party Seatrial images require an immutable digest")
    return value


def capture(source, identity):
    path = Path(source) / "plumb.toml"
    if not path.exists() and not path.is_symlink():
        return None
    checked(path.is_file() and not path.is_symlink(), "Seatrial declaration needs a regular plumb.toml")
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode())
    guard = document.get("guard", {})
    checked(isinstance(guard, dict), "guard must be a table")
    checked(set(guard) <= {"seatrial"}, "guard declares only the Seatrial suite")
    if "seatrial" not in guard:
        return None
    specification = validate(guard["seatrial"])
    checked(document.get("identity", {}).get("ns") == identity["repository"].split("/", 1)[1], "Seatrial declaration product differs from source")
    released = document.get("release", {})
    public = released.get("authority")
    authority(public)
    return {"schema": SCHEMA, "source": {name: identity[name] for name in ("repository", "marker", "commit", "tree")}, "manifest": hashlib.sha256(raw).hexdigest(), "authority": public, "specification": specification}


def bound(value, source):
    shape(value, ("schema", "source", "manifest", "authority", "specification"), "published Seatrial declaration")
    checked(value["schema"] == SCHEMA and value["source"] == source, "Seatrial declaration belongs to another source")
    checked(isinstance(value["manifest"], str) and re.fullmatch(r"[0-9a-f]{64}", value["manifest"]), "Seatrial declaration manifest digest is missing")
    authority(value["authority"])
    validate(value["specification"])
    return value
