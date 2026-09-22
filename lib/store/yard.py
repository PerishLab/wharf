import base64
import binascii
import json
import os
from pathlib import Path

from lib.media import depot
from lib.refusal import Refusal

SCHEMA = "wharf.yard/v1"
FIELDS = ("repository", "marker", "kind")


def key(repository, marker, kind, digest):
    if not depot.DIGEST.fullmatch(digest):
        raise Refusal(f"yard digest {digest!r} is not a sha256 hex digest")
    depot.directory(kind)
    return f"{repository}/{marker}/{kind}/{digest}.json"


def read(bucket, consigned):
    name = key(*(consigned[field] for field in FIELDS), consigned["digest"])
    if not bucket.exists(name):
        raise Refusal(f"the yard holds no {name}; consign it again, since yard objects expire")
    body = bucket.get(name)
    if depot.sha(body) != consigned["digest"]:
        raise Refusal(f"{name} does not hold the bytes its digest names")
    try:
        document = json.loads(body)
    except ValueError as broken:
        raise Refusal(f"{name} is not JSON: {broken}") from broken
    if document.get("schema") != SCHEMA:
        raise Refusal(f"{name} is not a {SCHEMA} consignment")
    for field in FIELDS:
        if document.get(field) != consigned[field]:
            raise Refusal(f"{name} names {field} {document.get(field)!r}, not {consigned[field]!r}")
    return document


def body(entry):
    try:
        held = base64.b64decode(entry["body"], validate=True)
    except (KeyError, binascii.Error) as broken:
        raise Refusal(f"yard object {entry.get('path')!r} carries no readable body") from broken
    if depot.sha(held) != entry.get("sha256"):
        raise Refusal(f"yard object {entry.get('path')!r} does not hold the bytes it names")
    return held


def unpack(document, target):
    target = Path(target)
    objects = document.get("objects") or []
    if not objects:
        raise Refusal("the consignment holds no objects")
    paths = [entry.get("path", "") for entry in objects]
    if len(set(paths)) != len(paths):
        raise Refusal("the consignment names one path twice")
    for entry in objects:
        depot.check(entry.get("path", ""))
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body(entry))
        os.chmod(path, 0o755 if entry.get("executable") else 0o644)
    return target
