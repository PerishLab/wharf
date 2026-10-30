import gzip
import io
import re
import struct
import tarfile

from lib.content import canonical
from lib.cargo.basis import WIDENED
from lib.identity import bind, elf, region
from lib.identity.format import LAYOUT, SIZE
from lib.identity.bind.spec import LIMIT, TARGET, entry, require, sha
from lib.media import archive
from lib.refusal import Refusal


def configuration(basis):
    require(set(basis) == {"entry", "toolchain", "manifest", "members", "lock", "config", "widened"} and basis["widened"] == WIDENED, "build basis is incomplete or has unknown widening")
    held = basis["entry"]
    require(set(held) == {"kind", "packages", "binaries", "target", "runner"} and held["kind"] == "cargo-binary" and held["target"] == TARGET and held["runner"] == "ubuntu-24.04", "build basis target or runner is unsupported")
    require(isinstance(held["packages"], list) and held["packages"] and all(isinstance(name, str) and name for name in held["packages"]) and len(set(held["packages"])) == len(held["packages"]), "build basis packages are incomplete")
    tools = basis["toolchain"]
    require(set(tools) == {"channel", "profile", "components", "targets"} and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(tools["channel"])) and tools["profile"] == "minimal" and tools["components"] == ["clippy", "rustfmt"] and tools["targets"] == [], "build basis toolchain is unsupported")
    require(set(basis["manifest"]) == {"Cargo.toml"} and isinstance(basis["members"], dict) and basis["members"] and set(basis["config"]) <= {".cargo/config.toml"}, "build basis source object coverage is incomplete")
    require(all(re.fullmatch(r"[0-9a-f]{40}", str(value)) for group in ("manifest", "members", "config") for value in basis[group].values()), "build basis source object identity is invalid")
    lock = basis["lock"]
    require(isinstance(lock, list) and lock and all(isinstance(row, list) and len(row) == 4 and all(isinstance(value, str) for value in row) and row[0] and row[1] and (not row[3] or re.fullmatch(r"[0-9a-f]{64}", row[3])) for row in lock), "build basis locked closure is incomplete")
    require(lock == sorted(lock) and len({tuple(row[:3]) for row in lock}) == len(lock), "build basis locked closure is duplicated or reordered")


def unbound(body, identity, workload):
    require(len(body) <= LIMIT and body[:4] == b"\x7fELF", "binary association supports bounded ELF files only")
    require(len(body) >= 64 and struct.unpack_from("<H", body, 18)[0] == 62, "binary association supports x86-64 ELF only")
    start, end = elf.locate(body)
    origin, binding = bind.inspect(body)
    expected = {"product": identity["repository"].split("/")[1], "marker": identity["marker"], "digest": canonical.digest(identity), "commit": identity["commit"], "workload": workload}
    require(binding == expected, "published binary binding differs from the exact release or workload")
    require(origin == {"prefix": expected["product"].upper().replace("-", "_"), "commit": "", "target": TARGET}, "published binary origin is unsupported")
    offset = LAYOUT["length"][0]
    original = body[:start] + body[start:start + offset] + bytes(SIZE - offset) + body[end:]
    require(bind.inspect(original) == (origin, None) and bind.bind(original, expected) == body, "binary association cannot reproduce exactly the permitted identity binding")
    return original, {"origin": origin, "region": {"start": start, "size": end - start}, "size": len(body), "bound": sha(body), "unbound": sha(original), "outside": sha(body[:start] + body[end:])}


def binaries(public, declaration, built, bound):
    names = declaration["built"]
    source = public["source"]
    expected_files = {name + "-" + TARGET for name in names} | {"receipt.json"}
    require(set(built["files"]) == expected_files and set(bound["files"]) == expected_files, "binary workload has missing or extra executables")
    basis = built["basis"]
    configuration(basis)
    require(basis.get("entry", {}).get("kind") == "cargo-binary" and basis["entry"].get("target") == TARGET and basis["entry"].get("binaries") == names, "build basis differs from the frozen declaration")
    require(bound["basis"] == {"entry": {"kind": "binary-identity", "binary": built["key"]}, "identity": source}, "binding basis differs from the build and release")
    receipt = built["receipt"]
    require(receipt.get("action") == "ship.binary" and receipt.get("product") == source["repository"].split("/")[1] and receipt.get("target") == TARGET and receipt.get("binaries") == names, "build receipt identity differs")
    require(receipt.get("toolchain") == basis.get("toolchain", {}).get("channel") and receipt.get("packages") == basis["entry"].get("packages") and isinstance(receipt.get("rustc"), str) and receipt["rustc"], "build receipt configuration is incomplete")
    require(receipt.get("files") == {name: entry(body) for name, body in built["files"].items() if name != "receipt.json"}, "build receipt bytes differ from recorded files")
    result = {}
    for name in names:
        filename = name + "-" + TARGET
        restored, facts = unbound(bound["files"][filename], source, built["key"])
        require(restored == built["files"][filename], "bound binary differs outside the exact permitted identity transformation")
        result[name] = facts
    receipt = bound["receipt"]
    expected_binding = bind.inspect(bound["files"][names[0] + "-" + TARGET])[1]
    require(receipt.get("action") == "ship.identity" and receipt.get("binding") == expected_binding, "binding receipt identity differs")
    expected = [{"file": name + "-" + TARGET, "origin": result[name]["origin"], "signature": None, **entry(bound["files"][name + "-" + TARGET])} for name in names]
    require(receipt.get("binaries") == expected, "binding receipt is incomplete or carries unsupported signing")
    return result


def unpack(body, names):
    try:
        return members(body, names)
    except (tarfile.TarError, OSError, EOFError) as failure:
        raise Refusal("Linux archive format is invalid or incomplete") from failure


def members(body, names):
    require(isinstance(body, bytes) and len(body) <= 2 * LIMIT, "Linux archive exceeds verification bounds")
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as compressed:
        expanded = compressed.read(4 * LIMIT + 1048577)
    require(len(expanded) <= 4 * LIMIT + 1048576, "Linux archive expansion exceeds verification bounds")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as tar:
        members = tar.getmembers()
        require([member.name for member in members] == names, "Linux archive has missing, extra, duplicate or reordered members")
        require(all(member.type == tarfile.REGTYPE and member.mode == 0o755 and member.uid == member.gid == member.mtime == 0 and member.uname == member.gname == "" and not member.pax_headers and not member.linkname and 0 < member.size <= LIMIT for member in members), "Linux archive metadata or member type is unsupported")
        require(sum(member.size for member in members) <= 4 * LIMIT, "Linux archive expanded content exceeds verification bounds")
        files = {member.name: tar.extractfile(member).read() for member in members}
    require(archive.tarball(list(files.items())) == body, "Linux archive representation is not the supported deterministic packaging")
    return files


def packaged(body, names, bound):
    files = unpack(body, names)
    require(all(value == bound["files"][name + "-" + TARGET] for name, value in files.items()), "public archive differs from the verified bound bytes")
    return {name: entry(value) for name, value in files.items()}
