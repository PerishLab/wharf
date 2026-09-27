import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib.content import declaration, resources
from lib.debian import control
from lib.process import git, run
from lib.refusal import Refusal

PLACEMENT = resources.read_json("releases.json")["placements"]["deb"]
PAYLOAD = "root"
CONTROL = "control"
SCRIPTS = ("preinst", "postinst", "prerm", "postrm")
MODES = {"100644": 0o644, "100755": 0o755}
UNITS = ("lib/systemd/system/", "usr/lib/systemd/system/")
EPOCH = 0


@dataclass(frozen=True)
class Declared:
    binary: str
    root: str


@dataclass(frozen=True)
class Package:
    source: Path
    declared: Declared
    bound: Path
    product: str
    version: str
    output: Path


def named(product):
    return f"{product}-{PLACEMENT['target']}.{PLACEMENT['format']}"


def declared(source, binary):
    table = declaration.release(source).get("deb")
    root = table.get("root") if isinstance(table, dict) else None
    relative = PurePosixPath(root) if isinstance(root, str) and root else None
    if relative is None or relative.is_absolute() or ".." in relative.parts or "\\" in root:
        raise Refusal("[release.deb].root must name a directory inside the product")
    return Declared(binary, relative.as_posix())


def placed(relative):
    head = relative.split("/", 1)[0]
    if relative in (CONTROL, *SCRIPTS) or (head == PAYLOAD and relative != PAYLOAD):
        return relative
    raise Refusal(f"a Debian root holds control, {PAYLOAD}/ and {', '.join(SCRIPTS)} alone, not {relative}")


def listing(source, root):
    held = {}
    for line in filter(None, git(source, "ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", f"{root}/").split("\0")):
        meta, path = line.split("\t", 1)
        mode, _, oid = meta.split()
        relative = placed(PurePosixPath(path).relative_to(root).as_posix())
        if mode not in MODES:
            raise Refusal(f"{path} is neither a regular file nor an executable; a Debian root refuses links and submodules")
        held[relative] = [mode, oid]
    if CONTROL not in held:
        raise Refusal(f"{root}/{CONTROL} is not tracked; a Debian root carries its control template")
    return held


def dpkg(runner=run):
    return runner(["dpkg-deb", "--version"], ".").splitlines()[0].strip()


def basis(source, held, binary, version):
    return {
        "entry": {"kind": "deb", "binary": binary, "executable": held.binary, "version": version, "architecture": PLACEMENT["architecture"]},
        "root": {"path": held.root, "files": listing(source, held.root)},
        "dpkg": dpkg(),
        "epoch": EPOCH,
    }


def regular(path):
    held = os.lstat(path)
    if not stat.S_ISREG(held.st_mode):
        raise Refusal(f"{path} is not a regular file; a Debian root refuses links and special files")
    return path.read_bytes()


def payload(listed):
    return {relative.split("/", 1)[1]: mode for relative, (mode, _) in listed.items() if relative.startswith(f"{PAYLOAD}/")}


def units(source, held):
    root = Path(source) / held.root / PAYLOAD
    return {f"/{path}": hashlib.sha256(regular(root / path)).hexdigest() for path in sorted(payload(listing(source, held.root))) if path.startswith(UNITS)}


def depends(source, held):
    return control.depends(regular(Path(source) / held.root / CONTROL).decode())


def placing(target, body, mode):
    if target.exists():
        raise Refusal(f"{target.name} is placed twice in the Debian package")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    target.chmod(mode)


def staged(package, listed, directory):
    root = Path(package.source) / package.declared.root
    for path, mode in sorted(payload(listed).items()):
        if path.split("/", 1)[0] == "DEBIAN":
            raise Refusal("a Debian payload cannot write DEBIAN/; wharf writes the control area")
        placing(directory / path, regular(root / PAYLOAD / path), MODES[mode])
    placing(directory / "usr/bin" / package.declared.binary, regular(Path(package.bound)), 0o755)
    text = control.rendered(regular(root / CONTROL).decode(), package.declared.binary, package.version, PLACEMENT["architecture"])
    placing(directory / "DEBIAN/control", text.encode(), 0o644)
    conffiles = sorted(f"/{path}" for path in payload(listed) if path.startswith("etc/"))
    if conffiles:
        placing(directory / "DEBIAN/conffiles", "".join(f"{path}\n" for path in conffiles).encode(), 0o644)
    for name in (name for name in SCRIPTS if name in listed):
        placing(directory / "DEBIAN" / name, regular(root / name), 0o755)
    for path in [directory, *directory.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (EPOCH, EPOCH), follow_symlinks=False)
    return conffiles


def build(package, runner=run):
    output = Path(package.output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    listed = listing(package.source, package.declared.root)
    with tempfile.TemporaryDirectory() as held:
        directory = Path(held) / "package"
        conffiles = staged(package, listed, directory)
        output.mkdir(parents=True)
        file = output / named(package.product)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH), TZ="UTC", LC_ALL="C")
        runner(["dpkg-deb", "--root-owner-group", "--threads-max=1", "-Zxz", "--build", str(directory), str(file)], held, env)
    body = file.read_bytes()
    receipt = {"action": "ship.deb", "file": file.name, "package": package.declared.binary, "version": package.version, "conffiles": conffiles, "dpkg": dpkg(runner), "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
