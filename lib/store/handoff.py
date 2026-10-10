import hashlib
from pathlib import Path

from lib.content import canonical, implementation
from lib.content.lane import preview
from lib.content.static import assets, evidence, runtime
from lib.refusal import Refusal
from lib.store import workload

MODULES = ("lib.media.node", "lib.content.static.assets", "lib.content.static.source", "lib.content.static.evidence", "lib.store.handoff")


def recipe(receipt):
    isolated = receipt.get("schema") == "wharf.preview.isolated/v1"
    fields = {"schema", "source", "app", "guard", "tools", "implementation"}
    preview.shape(receipt, fields | ({"execution"} if isolated else set()), "static build receipt")
    if receipt["schema"] not in ("wharf.preview.build/v1", "wharf.preview.isolated/v1"):
        raise Refusal("unsupported static build receipt")
    preview.shape(receipt["source"], {"repository", "commit", "tree", "declaration"}, "static build source")
    preview.matches(receipt["source"]["repository"], preview.REPOSITORY, "source repository")
    preview.source({key: value for key, value in receipt["source"].items() if key != "repository"})
    preview.slug(receipt["app"], "static app")
    preview.shape(receipt["tools"], set(evidence.TOOLS), "static tool world")
    for tool in receipt["tools"].values():
        preview.shape(tool, {"path", "sha256", "version"}, "static tool")
        if not isinstance(tool["path"], str) or not Path(tool["path"]).is_absolute():
            raise Refusal("static tool path must be absolute")
        preview.matches(tool["sha256"], preview.HEX[64], "tool digest")
        preview.text(tool["version"], "tool version")
    identity = {"source": receipt["source"], "repository": receipt["source"]["repository"]}
    evidence.guard(canonical.encode(receipt["guard"]), identity, receipt["tools"]["plumb"]["version"])
    if not isinstance(receipt["implementation"], dict) or not receipt["implementation"]:
        raise Refusal("static build receipt must bind its implementation")
    for name, digest in receipt["implementation"].items():
        if not isinstance(name, str) or not (name.startswith("lib.") or name in ("resource:registries.json", "resource:build.json")):
            raise Refusal("static implementation identity is malformed")
        preview.matches(digest, preview.HEX[64], "implementation digest")
    if isolated:
        execution(receipt)
    if receipt["implementation"] != world(isolated):
        raise Refusal("static build and consumer must use the same exact trusted implementation world")
    return receipt


def describe(files, receipt):
    recipe(receipt)
    identity = {"source": receipt["source"], "repository": receipt["source"]["repository"]}
    manifest = assets.manifest(files, identity)
    payload = dict(files, **{assets.IDENTITY: canonical.encode(manifest), "_headers": assets.HEADERS})
    entries = {name: {"blob": hashlib.sha256(body).hexdigest(), "size": len(body)} for name, body in sorted(payload.items())}
    basis = {"entry": {"kind": "static-preview-content"}, "receipt": receipt}
    key = workload.key(basis, receipt["implementation"])
    return {"schema": "wharf.preview.bundle/v1", "key": key, "basis": basis, "content": manifest["digest"], "files": entries}, payload


def pack(destination, files, receipt):
    document, payload = describe(files, receipt)
    destination = Path(destination)
    if destination.exists():
        raise Refusal("static handoff destination must be absent")
    destination.mkdir(parents=True)
    (destination / "bundle.json").write_bytes(canonical.encode(document))
    for body in payload.values():
        (destination / hashlib.sha256(body).hexdigest()).write_bytes(body)
    return document


def verify(document, bodies):
    preview.shape(document, {"schema", "key", "basis", "content", "files"}, "static bundle")
    if document["schema"] != "wharf.preview.bundle/v1":
        raise Refusal("unsupported static bundle schema")
    basis = document["basis"]
    preview.shape(basis, {"entry", "receipt"}, "static bundle basis")
    recipe(basis["receipt"])
    files = {}
    if not isinstance(document["files"], dict) or len(document["files"]) > 1002:
        raise Refusal("static bundle entries exceed the budget")
    for name, expected in document["files"].items():
        assets.path(name)
        preview.shape(expected, {"blob", "size"}, "static blob")
        preview.matches(expected["blob"], preview.HEX[64], "blob digest")
        if type(expected["size"]) is not int or not 0 < expected["size"] <= assets.MAX_FILE:
            raise Refusal("static blob size is malformed")
        body = bodies(expected["blob"])
        if len(body) != expected["size"] or hashlib.sha256(body).hexdigest() != expected["blob"]:
            raise Refusal("static handoff blob disagrees with its exact byte identity")
        files[name] = body
    source = {name: body for name, body in files.items() if name not in (assets.IDENTITY, "_headers")}
    rebuilt, payload = describe(source, basis["receipt"])
    if document != rebuilt or files != payload:
        raise Refusal("static handoff manifest, policy or workload identity disagrees")
    return files


def local(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise Refusal("static handoff must be a real directory")
    index = root / "bundle.json"
    if index.is_symlink() or not index.is_file() or index.stat().st_size > evidence.LIMIT:
        raise Refusal("static handoff index must be a bounded regular file")
    document = evidence.decode(index.read_bytes())

    def body(name):
        entry = root / name
        if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > assets.MAX_FILE:
            raise Refusal("static handoff blob must be a bounded regular file")
        return entry.read_bytes()

    verify(document, body)
    expected = {"bundle.json"} | {entry["blob"] for entry in document["files"].values()}
    if {entry.name for entry in root.iterdir()} != expected:
        raise Refusal("static handoff carries unselected files")
    return document


def publish(bucket, directory):
    document = local(directory)
    key, basis = document["key"], document["basis"]
    result = workload.publish(bucket, key, workload.Produced(Path(directory), basis, {"kind": "static-preview-content"}))
    fetch(bucket, key)
    return result


def fetch(bucket, key):
    prefix = workload.prefix(key)
    record = evidence.decode(bucket.get(prefix + "record.json"))
    if not isinstance(record, dict) or record.get("hash_version") != workload.HASH_VERSION or record.get("key") != key or not isinstance(record.get("files"), dict):
        raise Refusal("static handoff workload record is malformed")

    def body(name):
        preview.matches(name, preview.HEX[64], "blob key")
        expected = record["files"].get(name)
        if not isinstance(expected, dict) or type(expected.get("size")) is not int or not 0 < expected["size"] <= assets.MAX_FILE:
            raise Refusal("static handoff record has no bounded blob")
        held = bucket.get(prefix + "blobs/" + name)
        if len(held) != expected["size"] or hashlib.sha256(held).hexdigest() != expected.get("sha256"):
            raise Refusal("static handoff blob does not match its workload record")
        return held

    index = bucket.get(prefix + "blobs/bundle.json")
    expected_index = record["files"].get("bundle.json")
    if not isinstance(expected_index, dict) or expected_index.get("size") != len(index) or expected_index.get("sha256") != hashlib.sha256(index).hexdigest():
        raise Refusal("static handoff index disagrees with its workload record")
    document = evidence.decode(index)
    files = verify(document, body)
    if document["key"] != key or bucket.get(prefix + "basis.json") != canonical.encode(document["basis"]):
        raise Refusal("static handoff key or basis disagrees with its immutable address")
    expected = {"bundle.json"} | {entry["blob"] for entry in document["files"].values()}
    if set(record["files"]) != expected:
        raise Refusal("static handoff workload contains unselected blobs")
    return document, files


def execution(receipt):
    held = receipt["execution"]
    preview.shape(held, {"controls", "phases"}, "isolated Preview execution")
    preview.shape(held["controls"], {"plumb", "ectropy", "control.py"}, "isolated controls")
    for digest in held["controls"].values():
        preview.matches(digest, preview.HEX[64], "control digest")
    preview.shape(held["phases"], {"install", "guard", "build"}, "isolated phases")
    worlds = list(held["phases"].values())
    for value in worlds:
        observed(value)
    if any(value != worlds[0] for value in worlds) or worlds[0].get("profile") != runtime.POLICY:
        raise Refusal("isolated Preview phases disagree with their exact execution profile")
    for name in ("plumb", "ectropy"):
        if receipt["tools"][name]["sha256"] != held["controls"][name]:
            raise Refusal("isolated Preview controls disagree with invoked tools")
    if held["controls"]["control.py"] != implementation.digest("lib.content.static.control")["lib.content.static.control"]:
        raise Refusal("isolated Preview control script disagrees with the trusted implementation")


def observed(value):
    preview.shape(value, {"image", "profile", "engine", "client", "implementation"}, "isolated runtime")
    preview.shape(value["image"], {"requested", "resolved", "id"}, "isolated image")
    image = value["image"]
    prefix = runtime.POLICY["image"].removesuffix(":stable") + "@sha256:"
    if image["requested"] != runtime.POLICY["image"] or not isinstance(image["resolved"], str) or not image["resolved"].startswith(prefix):
        raise Refusal("isolated runtime image is outside the trusted public identity")
    preview.matches(image["resolved"].removeprefix(prefix), preview.HEX[64], "image digest")
    if not isinstance(image["id"], str) or not image["id"].startswith("sha256:"):
        raise Refusal("isolated image identifier is malformed")
    preview.matches(image["id"].removeprefix("sha256:"), preview.HEX[64], "image identifier")
    preview.shape(value["client"], {"path", "sha256"}, "isolated engine client")
    if not isinstance(value["client"]["path"], str) or not Path(value["client"]["path"]).is_absolute():
        raise Refusal("isolated engine client must have an absolute path")
    preview.matches(value["client"]["sha256"], preview.HEX[64], "engine client digest")
    preview.shape(value["engine"], {"ServerVersion", "KernelVersion", "Architecture", "SecurityOptions"}, "isolated engine")
    for name in ("ServerVersion", "KernelVersion", "Architecture"):
        preview.text(value["engine"][name], "engine identity")
    options = value["engine"]["SecurityOptions"]
    if not isinstance(options, list) or any(not isinstance(option, str) for option in options) or not any("seccomp" in option and "unconfined" not in option for option in options):
        raise Refusal("isolated engine lacks the trusted seccomp boundary")
    if value["implementation"] != implementation.resourced(["lib.content.static.runtime"], ["build.json"]):
        raise Refusal("isolated runtime implementation disagrees with the consumer")


def world(isolated=False):
    modules = MODULES + (("lib.content.static.bridge", "lib.content.static.control") if isolated else ())
    return implementation.resourced(modules, ["registries.json"] + (["build.json"] if isolated else []))
