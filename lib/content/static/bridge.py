import hashlib
import shutil
import tempfile
import zlib
from pathlib import Path

from lib.content import implementation, preview
from lib.content.static import assets, evidence, runtime, source, workspace
from lib.refusal import Refusal
from lib.store import handoff

PHASES = ("install", "guard", "build")


def controls(directory):
    root = runtime.directory(Path(directory))
    for name in ("plumb", "ectropy"):
        path = Path(shutil.which(name)).resolve()
        if not path.is_file():
            raise Refusal("Preview requires prepared stable controls")
        shutil.copy2(path, root / name)
    shutil.copy2(implementation.source("lib.content.static.control"), root / "control.py")
    return fingerprints(root)


def fingerprints(root):
    result = {}
    for entry in Path(root).iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise Refusal("Preview controls must be regular read-only guest files")
        result[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    if set(result) != {"plumb", "ectropy", "control.py"}:
        raise Refusal("Preview controls contain unexpected files")
    return result


def phase(held, name, execute):
    seat, request, control, expected = (held[key] for key in ("seat", "request", "controls", "expected"))
    env = workspace.environment(tempfile.gettempdir())
    if metadata(seat.root) != held["metadata"]:
        raise Refusal("Preview Git control metadata changed before host inspection")
    if source.qualify(seat.root, request.intent, request.target, env) != seat.configuration or fingerprints(control) != expected:
        raise Refusal("Preview source or controls changed between guest phases")
    selectors = {"PLUMB_GUARD_STRENGTH": "full", "PLUMB_GUARD_BOUNDARY": "head", "CI": "true", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    command = ["/usr/bin/python3", "/controls/control.py", name, seat.configuration["package"]["name"]]
    try:
        completed = execute(runtime.Guest(seat.root, control, command, selectors))
    except runtime.Uncertain:
        seat.quiescent = False
        raise
    if metadata(seat.root) != held["metadata"]:
        raise Refusal("Preview Git control metadata changed during guest execution")
    document = evidence.decode(completed.stdout)
    preview.shape(document, {"phase", "tools", "guard"}, "Preview guest phase")
    if document["phase"] != name or fingerprints(control) != expected:
        raise Refusal("Preview phase or controls disagree with the invocation")
    if source.qualify(seat.root, request.intent, request.target, env) != seat.configuration:
        raise Refusal("Preview source changed during guest execution")
    output = source.output(seat.root, seat.configuration["directory"])
    if name == "install" and output.exists():
        raise Refusal("Preview output appeared before the build phase")
    if held.get("tools") is not None and document["tools"] != held["tools"]:
        raise Refusal("Preview guest tool world changed between phases")
    held["tools"] = document["tools"]
    preview.shape(document["tools"], set(evidence.TOOLS), "Preview guest tools")
    for tool in document["tools"].values():
        preview.shape(tool, {"path", "sha256", "version"}, "Preview guest tool")
        preview.matches(tool["sha256"], preview.HEX[64], "Preview guest tool digest")
    for tool in ("plumb", "ectropy"):
        if document["tools"][tool]["sha256"] != expected[tool]:
            raise Refusal("Preview guest control identity differs from trusted bytes")
    proof = None
    if name == "guard":
        proof = evidence.guard(document["guard"], request.intent, document["tools"]["plumb"]["version"])
        if proof["root"] != "/source":
            raise Refusal("Preview Guard observed a different guest source")
        if output.exists():
            assets.collect(output)
            shutil.rmtree(output)
    elif document["guard"] is not None:
        raise Refusal("Unexpected Guard data outside the Guard phase")
    return completed.runtime, proof


def metadata(root):
    directory = Path(root) / ".git"
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal("Preview Git metadata must remain independent and regular")
    result = {}
    for entry in directory.rglob("*"):
        name = str(entry.relative_to(directory))
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise Refusal("Preview Git metadata contains a non-regular entry")
        if entry.is_file() and name != "index" and not object(entry, name):
            result[name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return result


def object(entry, name):
    parts = name.split("/")
    if len(parts) != 3 or parts[0] != "objects" or len(parts[1]) != 2 or len(parts[2]) != 38:
        return False
    preview.matches(parts[1] + parts[2], preview.HEX[40], "Git object name")
    if entry.stat().st_size > assets.MAX_FILE:
        raise Refusal("Preview Git object exceeds its bounded identity budget")
    try:
        decoder = zlib.decompressobj()
        body = decoder.decompress(entry.read_bytes(), assets.MAX_FILE + 1)
    except zlib.error as error:
        raise Refusal("Preview Git object is malformed") from error
    if len(body) > assets.MAX_FILE or not decoder.eof or decoder.unused_data or hashlib.sha1(body).hexdigest() != parts[1] + parts[2]:
        raise Refusal("Preview Git object disagrees with its content identity")
    return True


def build(request, destination, execute=runtime.run):
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists() or destination.resolve().is_relative_to(Path(request.root).resolve()):
        raise Refusal("Preview handoff must be absent and outside canonical source")
    identity = handoff.world(True)
    records = {}
    with tempfile.TemporaryDirectory(prefix="wharf-preview-controls-") as temporary:
        control = Path(temporary)
        expected = controls(control)
        with workspace.prepare(request) as seat:
            held = {"seat": seat, "request": request, "controls": control, "expected": expected, "metadata": metadata(seat.root)}
            for name in PHASES:
                world, proof = phase(held, name, execute)
                if records and world != records["install"]:
                    raise Refusal("Preview runtime changed between phases")
                records[name] = world
                if proof is not None:
                    guarded = proof
            if handoff.world(True) != identity:
                raise Refusal("Preview bridge implementation changed during execution")
            files = assets.collect(source.output(seat.root, seat.configuration["directory"]))
            origin = dict(request.intent["source"], repository=request.intent["repository"])
            receipt = {"schema": "wharf.preview.isolated/v1", "source": origin, "app": request.intent["app"], "guard": guarded,
                       "tools": held["tools"], "implementation": identity, "execution": {"controls": expected, "phases": records}}
            handoff.recipe(receipt)
        if fingerprints(control) != expected or handoff.world(True) != identity:
            raise Refusal("Preview controls or implementation changed before handoff")
    return handoff.pack(destination, files, receipt)
