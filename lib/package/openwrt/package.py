import hashlib
import json
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib.content import declaration, resources
from lib.process import git, run
from lib.refusal import Refusal

PROFILE = resources.read_json("releases.json")["placements"]["apk"]
BUILDER = resources.read_json("build/config.json")["apk"]["builder"]
MODES = {"100644": 0o644, "100755": 0o755}


@dataclass(frozen=True)
class Declared:
    binary: str
    root: str


@dataclass(frozen=True)
class Package:
    source: Path
    declared: Declared
    bound: Path
    version: str
    output: Path


def named(binary, version):
    return f"{binary}-{version}.apk"


def declared(source, binary):
    held = declaration.release(source).get("apk")
    if not isinstance(held, dict) or held.get("profile") != "openwrt-aarch64-cortex-a53":
        raise Refusal("OpenWrt APK requires the declared openwrt-aarch64-cortex-a53 profile")
    root = held.get("root")
    relative = PurePosixPath(root) if isinstance(root, str) and root else None
    if relative is None or relative.is_absolute() or str(relative) != root or any(part in (".", "..") for part in relative.parts) or "\\" in root:
        raise Refusal("OpenWrt APK root must name a normalized directory inside the product")
    return Declared(binary, root)


def listing(source, root):
    held = {}
    for line in filter(None, git(source, "ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", f"{root}/").split("\0")):
        meta, path = line.split("\t", 1)
        mode, _, oid = meta.split()
        relative = PurePosixPath(path).relative_to(root).as_posix()
        if mode not in MODES or (relative != "package.toml" and not relative.startswith("root/")):
            raise Refusal("OpenWrt APK carries package.toml and regular root/ files only")
        if relative.startswith("root/etc/rc.d/"):
            raise Refusal("OpenWrt APK cannot enable services during installation")
        held[relative] = [mode, oid]
    if "package.toml" not in held:
        raise Refusal("OpenWrt APK root carries its tracked package.toml")
    return held


def regular(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise Refusal(f"{path} is not a regular OpenWrt package file")
    return path.read_bytes()


def metadata(source, held):
    return tomllib.loads(regular(Path(source) / held.root / "package.toml").decode())


def basis(source, held, binary, version):
    return {
        "entry": {"kind": "apk", "binary": binary, "executable": held.binary, "version": version, "architecture": PROFILE["architecture"]},
        "root": {"path": held.root, "files": listing(source, held.root)},
        "builder": BUILDER,
        "compat": "3.0.5",
        "compression": "deflate:9",
        "epoch": 0,
    }


def placing(path, body, mode):
    if path.exists():
        raise Refusal(f"OpenWrt APK places {path.name} twice")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)


def staged(package, directory):
    root = Path(package.source) / package.declared.root
    for name, (mode, _) in listing(package.source, package.declared.root).items():
        if name.startswith("root/"):
            placing(directory / name, regular(root / name), MODES[mode])
    placing(directory / "root/usr/bin" / package.declared.binary, regular(Path(package.bound)), 0o755)
    (directory / "build.sh").write_bytes(resources.read_bytes("build/apk.sh"))
    return metadata(package.source, package.declared)


def container(image, mounts):
    argv = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m"]
    for source, target, readonly in mounts:
        argv += ["--mount", f"type=bind,src={Path(source).resolve()},dst={target}" + (",readonly" if readonly else "")]
    return [*argv, image]


def build(package, runner=run):
    output = Path(package.output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        held = staged(package, directory)
        args = ["--info", f"name:{package.declared.binary}", "--info", f"version:{package.version}", "--info", f"arch:{PROFILE['architecture']}"]
        args += [part for key in ("description", "license") for part in ("--info", f"{key}:{held[key]}")]
        if held.get("depends"):
            args += ["--info", f"depends:{' '.join(held['depends'])}"]
        argv = container(BUILDER, [(directory, "/input", True), (output, "/output", False)])
        filename = named(package.declared.binary, package.version)
        runner([*argv, "/bin/sh", "/input/build.sh", "--output", f"/output/{filename}", *args], ".")
    body = (output / filename).read_bytes()
    receipt = {"action": "ship.apk", "file": filename, "package": package.declared.binary, "version": package.version, "architecture": PROFILE["architecture"], "builder": BUILDER, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "signed": False}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
