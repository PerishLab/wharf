import base64
import gzip
import re
import tempfile
from pathlib import PurePosixPath

from lib.content import canonical
from lib.identity.bind.spec import entry, require
from lib.identity.guard import material
from lib.process.confirmation import materials
from lib.store import plan


def validate(rows):
    require(isinstance(rows, list) and len(rows) <= 16, "original source recovery exceeds its file budget")
    names = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "oid", "edits"}, "original source recovery fields differ")
        path = PurePosixPath(row["path"]) if isinstance(row["path"], str) else None
        require(path is not None and not path.is_absolute() and path.as_posix() == row["path"] and ".." not in path.parts and "\\" not in row["path"] and "\0" not in row["path"] and path.name in material.LEAVES and row["path"] not in names, "original source recovery needs distinct dependency-file paths")
        require(isinstance(row["oid"], str) and re.fullmatch(r"[0-9a-f]{40}", row["oid"]), "original source recovery has no exact original Git blob")
        require(isinstance(row["edits"], list) and 1 <= len(row["edits"]) <= 32, "original source recovery edit budget differs")
        for edit in row["edits"]:
            require(isinstance(edit, dict) and set(edit) == {"before", "after"} and all(isinstance(edit[name], str) and 0 < len(edit[name].encode()) <= 8192 and "\0" not in edit[name] for name in ("before", "after")), "original source recovery needs bounded literal replacements")
        names.add(row["path"])
    return rows


def archived(root, source):
    require(source["repository"] in ("PerishLab/plumb", "PerishLab/concord"), "original source supports exact pilot repositories")
    require(re.fullmatch(r"[0-9a-f]{40}", source["commit"]) and re.fullmatch(r"[0-9a-f]{40}", source["tree"]), "original source Git identity differs")
    materials.git(root, "init", "--bare", "--initial-branch=main", "--template=")
    materials.git(root, "-c", "protocol.file.allow=never", "fetch", "--no-tags", "--depth=1", f"https://github.com/{source['repository']}.git", source["commit"])
    require(materials.git(root, "rev-parse", "FETCH_HEAD").decode().strip() == source["commit"] and materials.git(root, "rev-parse", "FETCH_HEAD^{tree}").decode().strip() == source["tree"], "original archived source differs from recorded publication")
    materials.git(root, "read-tree", source["tree"])


def replace(root, row):
    listing = materials.listed(materials.git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", "FETCH_HEAD", "--", row["path"]))
    require(len(listing) == 1 and listing[0]["path"] == row["path"] and listing[0]["oid"] == row["oid"], "original recovery path or exact archived blob differs")
    raw = materials.git(root, "cat-file", "blob", row["oid"])
    require(len(raw) <= material.LIMIT, "original dependency file exceeds its byte budget")
    for edit in row["edits"]:
        before, after = edit["before"].encode(), edit["after"].encode()
        require(raw.count(before) == 1, "original recovery replacement is absent or ambiguous")
        raw = raw.replace(before, after)
        require(len(raw) <= material.LIMIT, "original dependency file exceeds its byte budget")
    oid = materials.git(root, "hash-object", "-w", "--stdin", data=raw).decode().strip()
    materials.git(root, "update-index", "--cacheinfo", listing[0]["mode"], oid, row["path"])


def restored(root, recorded, world):
    require(recorded.get("snapshot") == canonical.digest(plan.combination(world)), "original recorded dependency material names another combination")
    for row in material.rows(recorded):
        path = PurePosixPath(row["path"])
        require(not path.is_absolute() and path.as_posix() == row["path"] and ".." not in path.parts and "\\" not in row["path"] and "\0" not in row["path"] and path.name in material.LEAVES, "original recorded material path differs")
        listing = materials.listed(materials.git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", "FETCH_HEAD", "--", row["path"]))
        require(len(listing) == 1 and listing[0]["path"] == row["path"] and listing[0]["mode"] == row["mode"], "original recorded dependency file or mode differs")
        oid = materials.git(root, "hash-object", "-w", "--stdin", data=row["content"].encode()).decode().strip()
        materials.git(root, "update-index", "--cacheinfo", row["mode"], oid, row["path"])


def packed(root, world):
    require(materials.git(root, "write-tree").decode().strip() == world["tree"], "complete recovered Git tree differs from original frozen world")
    records = materials.listed(materials.git(root, "ls-tree", "-r", "-l", "-z", "--full-tree", world["tree"]))
    raw = materials.git(root, "cat-file", "--batch", data="".join(row["oid"] + "\n" for row in records).encode())
    inventory = canonical.encode(materials.decoded(records, raw))
    require(len(inventory) <= materials.LIMIT * 2, "original source expanded inventory exceeds bounds")
    result = {"schema": "wharf.release.source/v1", "source": world["source"], "head": world["head"], "tree": world["tree"], "files": len(records), "payload": {"encoding": "gzip+base64", **entry(inventory), "data": base64.b64encode(gzip.compress(inventory, mtime=0)).decode()}}
    require(len(canonical.encode(result)) <= 4194304, "original source payload exceeds semantic object budget")
    return result


def recover(world, rows, recorded=None):
    validate(rows)
    require(recorded is None or not rows, "recorded original material cannot be overridden by recovery data")
    with tempfile.TemporaryDirectory(prefix="wharf-original-source-") as directory:
        archived(directory, world["source"])
        if recorded is not None:
            restored(directory, recorded, world)
        else:
            for row in rows:
                replace(directory, row)
        return packed(directory, world)
