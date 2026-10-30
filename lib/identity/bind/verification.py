import json
import tempfile
from pathlib import Path

from lib.content import canonical, implementation
from lib.identity.bind import artifacts, comparison, control, storage
from lib.identity.bind.spec import BIND, CARGO, FIELDS, TARGET, require, sha
from lib.media import release
from lib.store import plan


def planned(bucket, request):
    raw = bucket.get(plan.location(request))
    require(len(raw) <= 1048576, "candidate plan exceeds verification bounds")
    document = json.loads(raw)
    require(raw == canonical.encode(document) and type(document.get("schema")) is int and document["schema"] == 1, "candidate plan is not canonical schema 1")
    context = document["context"]
    require(all(context.get(name) == request[name] for name in ("repository", "marker", "run", "attempt")), "candidate plan belongs to another request")
    source = control.source({name: context.get(name) for name in FIELDS})
    producer = control.observer({name: context.get(name) for name in ("run", "attempt", "wharf")})
    control.trusted(producer)
    require(plan.combination(context["snapshot"])["source"] == source, "candidate snapshot belongs to another source")
    return document, dict(producer, sha256=sha(raw))


def chain(bucket, source, document, declaration):
    context = document["context"]
    require({name: context.get(name) for name in FIELDS} == source and plan.combination(context["snapshot"])["source"] == source, "binary plan or snapshot belongs to another source")
    entries = document["entries"]
    require(all(name in entries and isinstance(entries[name].get("key"), str) for name in ("binary-linux", "bind-linux")), "publication plan has no complete Linux binary chain")
    built = storage.read(bucket, entries["binary-linux"]["key"], implementation.resourced(*CARGO))
    bound = storage.read(bucket, entries["bind-linux"]["key"], implementation.resourced(*BIND))
    require(built["basis"].get("toolchain", {}).get("channel") == context["snapshot"]["domain"].get("rust.version"), "build toolchain differs from the complete publication resolution")
    for loaded, name in ((built, "binary-linux"), (bound, "bind-linux")):
        require(loaded["record"].get("context", {}).get("repository") == source["repository"], "binary producer belongs to another product")
        storage.producer(bucket, loaded, name)
    return {"declaration": declaration, "build": built, "binding": bound, "binaries": artifacts.binaries({"source": source}, declaration, built, bound)}


def packaged(source, facts):
    _, authority, _ = control.location(source["repository"], source["marker"])
    declaration = facts["declaration"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, body in facts["binding"]["files"].items():
            (root / name).write_bytes(body)
        contents = release.Contents({TARGET: root}, root, {TARGET: declaration["installed"]}, {})
        objects = release.objects(source["repository"].split("/")[1], contents, authority)
    require(set(objects) == {"linux-x64"}, "candidate packaging has unsupported archive coverage")
    _, body, reference = objects["linux-x64"]
    return body, {"archive": reference, "files": artifacts.packaged(body, declaration["installed"], facts["binding"])}


def published(repository, version):
    _, authority, root = control.location(repository, version)
    url = f"{authority}/{root}/build/linux-x64.json"
    before = release.fetch(url)
    proof, restored = comparison.read(repository, version)
    require(before == release.fetch(url), "reference discovery changed during candidate verification")
    reference = json.loads(before)["proof"]
    require(sha(canonical.encode(proof)) == reference["sha256"], "reference proof is not canonical")
    return proof, restored, reference
