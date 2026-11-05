import json
import platform
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from lib.cargo import toolchain
from lib.content import canonical
from lib.content.static import evidence
from lib.identity.guard import mismatch, snapshot
from lib.media import release
from lib.process import git
from lib.refusal import Refusal
from lib.store import plan

SCHEMA = "wharf.release.material/v1"
LEAVES = {"Cargo.toml", "Cargo.lock", "package.json", "pnpm-lock.yaml"}
LIMIT = 524288
MODES = {"100644", "100755"}


def path(root, name):
    relative = PurePosixPath(name) if isinstance(name, str) and name else None
    if relative is None or relative.is_absolute() or str(relative) != name or "\\" in name or "\0" in name or any(part in (".", "..") for part in relative.parts) or relative.name not in LEAVES:
        raise Refusal("release material names only normalized dependency files")
    target = root / name
    try:
        if target.resolve() != target or not stat.S_ISREG(target.lstat().st_mode):
            raise Refusal("release material requires existing regular dependency files without links")
    except (OSError, ValueError) as error:
        raise Refusal("release material dependency file is unavailable") from error
    return target


def rows(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema", "snapshot", "files"} or payload["schema"] != SCHEMA or not isinstance(payload["files"], list) or len(payload["files"]) > 64:
        raise Refusal("release material fields, schema or file count differ")
    names, size = set(), 0
    for row in payload["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "mode", "content"} or not isinstance(row["path"], str) or not isinstance(row["content"], str) or not isinstance(row["mode"], str) or row["mode"] not in MODES or row["path"] in names:
            raise Refusal("release material dependency file is malformed or repeated")
        names.add(row["path"])
        try:
            size += len(row["content"].encode("utf-8"))
        except UnicodeError as error:
            raise Refusal("release material dependency bytes are not UTF-8") from error
    if size > LIMIT:
        raise Refusal("release material exceeds its 512 KiB dependency-file budget")
    return payload["files"]


def capture(source, verified):
    root = Path(source).resolve()
    if git(root, "rev-parse", "HEAD").strip() != verified["head"] or git(root, "rev-parse", "HEAD^{tree}").strip() != verified["tree"]:
        raise Refusal("release material source differs from its verified snapshot")
    files, size = [], 0
    for name in filter(None, git(root, "diff", "--name-only", "-z", verified["source"]["commit"], "HEAD").split("\0")):
        target = path(root, name)
        size += target.stat().st_size
        if size > LIMIT:
            raise Refusal("release material exceeds its 512 KiB dependency-file budget")
        mode = git(root, "ls-tree", "HEAD", "--", name).split()[0]
        original = git(root, "ls-tree", verified["source"]["commit"], "--", name).split()
        if not original or original[0] != mode:
            raise Refusal("release material cannot create dependency files or change their mode")
        files.append({"path": name, "mode": mode, "content": target.read_bytes().decode("utf-8")})
    payload = {"schema": SCHEMA, "snapshot": canonical.digest(plan.combination(verified)), "files": files}
    rows(payload)
    return payload


def restore(held, payload):
    root = held["root"]
    for row in rows(payload):
        target = path(root, row["path"])
        original = snapshot.git(held, "ls-tree", "HEAD", "--", row["path"]).split()
        if not original or original[0] != row["mode"]:
            raise Refusal("release material dependency mode differs from its marker")
        target.write_bytes(row["content"].encode("utf-8"))
    snapshot.git(held, "add", "--update")
    if snapshot.git(held, "write-tree") != held["request"].expected["tree"]:
        raise Refusal("release material complete tree differs from the verified plan")


def qualify(request, payload):
    expected = request.expected
    combination = plan.combination(expected)
    if combination["source"] != request.identity or combination["domain"] != toolchain.checked(request.domain):
        raise Refusal("release material belongs to another source or domain")
    rows(payload)
    if payload["snapshot"] != canonical.digest(combination):
        raise Refusal("release material belongs to another verified combination")
    controls = expected["controls"]
    if not isinstance(controls, dict) or set(controls) != set(mismatch.GROUPS["controls"]) or any(not isinstance(controls[name], str) or not controls[name].startswith(name + " v") for name in ("plumb", "ectropy")):
        raise Refusal("release material control identities are malformed")
    context = {"identity": request.identity, "plumb": controls["plumb"], "authority": controls["authority"]}
    mismatch.qualified(context, expected["head"], {"tree": expected["tree"], "packages": expected["packages"]}, json.dumps(expected["guard"]))
    return expected


def current(controls):
    if mismatch.stable(controls, release.fetch) != controls["stable"]:
        raise Refusal("release material control authorities changed; prepare a fresh plan")


@contextmanager
def prepared(request, payload, compiling, inspect=evidence.inspect):
    if sys.platform != "linux" or platform.machine().lower() not in {"aarch64", "arm64"}:
        raise Refusal("closed ARM64 material consumption requires native Linux ARM64")
    expected = qualify(request, payload)
    source = Path(request.source).resolve()
    with tempfile.TemporaryDirectory(prefix="wharf-material-") as temporary, snapshot.checkout(source, request.destination or Path(temporary) / "product") as root:
        directory = Path(temporary)
        home = directory / "home"
        home.mkdir()
        env = snapshot.environment(home, request.domain)
        search = env["PATH"]
        tools = evidence.tools(source, env, inspect, ("git",))
        held = {"request": request, "root": directory, "env": env, "tools": tools, "inspect": inspect}
        snapshot.git(held, "clone", "--no-hardlinks", "--no-checkout", "--local", str(source), str(root))
        held["root"] = root
        snapshot.git(held, "checkout", "--detach", request.identity["commit"])
        snapshot.git(held, "remote", "set-url", "origin", f"https://github.com/{request.identity['repository']}.git")
        if snapshot.git(held, "rev-parse", "HEAD^{tree}") != request.identity["tree"]:
            raise Refusal("release material marker differs from its declared source")
        if compiling:
            snapshot.rust(source, (tools, env, search), inspect)
            if tools["rustc"]["version"].split()[1] != request.domain["rust.version"]:
                raise Refusal("release material requires the planned domain Rust")
        restore(held, payload)
        head = snapshot.committed(held, {"tree": expected["tree"]})
        if head != expected["head"]:
            raise Refusal("release material synthetic head differs from its verified plan")
        def confirm():
            snapshot.unchanged(held, head, expected["tree"])
            current(expected["controls"])
        confirm()
        yield root, expected, confirm
        confirm()
