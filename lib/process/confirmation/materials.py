import base64
import gzip
import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from lib.content import canonical
from lib.content.static import evidence
from lib.identity.bind.spec import entry, require
from lib.refusal import Refusal

LIMIT = 33554432


def git(root, *arguments, data=None):
    return subprocess.check_output(["git", "-C", str(root), *arguments], input=data, env=evidence.clean(os.environ, Path.home()), timeout=30)


def text(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeError as error:
        raise Refusal("effective source Git text is not UTF-8") from error


def listed(raw):
    require(0 < len(raw) <= 4194304 and raw.endswith(b"\0"), "effective source listing exceeds bounds")
    records = raw[:-1].split(b"\0")
    require(0 < len(records) <= 4096, "effective source file count exceeds bounds")
    found, paths, total = [], set(), 0
    for record in records:
        fields, separator, name = record.partition(b"\t")
        values = text(fields).split()
        require(separator == b"\t" and len(values) == 4, "effective source Git listing fields differ")
        mode, kind, oid, size = values
        path = text(name)
        relative = PurePosixPath(path)
        require(mode in ("100644", "100755") and kind == "blob" and re.fullmatch(r"[0-9a-f]{40}", oid), "effective source needs exact regular Git blobs")
        require(path and not relative.is_absolute() and relative.as_posix() == path and ".." not in relative.parts and "\\" not in path and all(ord(char) >= 32 for char in path), "effective source path is not portable")
        require(path.casefold() not in paths, "effective source paths have portable aliases")
        paths.add(path.casefold())
        require(size.isascii() and size.isdigit() and len(size) <= 12, "effective source Git blob size is invalid")
        size = int(size)
        require(0 <= size <= LIMIT, "effective source blob exceeds bounds")
        total += size
        require(total <= LIMIT, "effective source total exceeds bounds")
        found.append({"path": path, "mode": mode, "oid": oid, "size": size})
    return found


def decoded(records, raw):
    require(len(raw) <= LIMIT + 4096 * 100, "effective source blob response exceeds bounds")
    found, offset = [], 0
    for record in records:
        ending = raw.find(b"\n", offset)
        require(ending >= offset, "effective source blob header is incomplete")
        values = text(raw[offset:ending]).split()
        require(len(values) == 3, "effective source Git blob header fields differ")
        oid, kind, size = values
        require(size.isascii() and size.isdigit() and len(size) <= 12, "effective source Git blob response size is invalid")
        require(oid == record["oid"] and kind == "blob" and int(size) == record["size"], "effective source Git blob identity differs")
        offset = ending + 1
        data = raw[offset:offset + record["size"]]
        offset += record["size"]
        require(raw[offset:offset + 1] == b"\n", "effective source Git blob body is incomplete")
        offset += 1
        require(hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == oid, "effective source Git blob content differs")
        found.append({**record, **entry(data), "data": base64.b64encode(data).decode()})
    require(offset == len(raw), "effective source has unselected blob data")
    return found


def capture(root, source, world):
    require(world.get("source") == source, "effective source publication identity differs from actual world")
    require(git(root, "rev-parse", "HEAD").decode().strip() == world["head"] and git(root, "rev-parse", "HEAD^{tree}").decode().strip() == world["tree"], "effective source checkout differs from actual world")
    records = listed(git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"))
    raw = git(root, "cat-file", "--batch", data="".join(record["oid"] + "\n" for record in records).encode())
    inventory = canonical.encode(decoded(records, raw))
    require(len(inventory) <= LIMIT * 2, "effective source encoded inventory exceeds bounds")
    payload = {"schema": "wharf.release.source/v1", "source": source, "head": world["head"], "tree": world["tree"], "files": len(records), "payload": {"encoding": "gzip+base64", **entry(inventory), "data": base64.b64encode(gzip.compress(inventory, mtime=0)).decode()}}
    require(len(canonical.encode(payload)) <= 4194304, "effective source payload exceeds semantic object bounds")
    return payload
