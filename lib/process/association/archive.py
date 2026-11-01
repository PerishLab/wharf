import gzip
import io
import tarfile
import zipfile

from lib.media import archive
from lib.process.association.spec import LIMIT, require, target
from lib.refusal import Refusal


def members(body, names, selected):
    require(isinstance(body, bytes) and 0 < len(body) <= 2 * LIMIT and names and len(set(names)) == len(names), "native archive bounds or inventory differs")
    if target(selected)["format"] == "zip":
        with zipfile.ZipFile(io.BytesIO(body)) as held:
            rows = held.infolist()
            require([row.filename for row in rows] == [name + ".exe" for name in names], "native zip has missing, extra, duplicate or reordered members")
            require(all(0 < row.file_size <= LIMIT and row.flag_bits == 0 and row.compress_type == zipfile.ZIP_DEFLATED and row.date_time == archive.EPOCH and row.external_attr == (0o100755 << 16) and not row.extra and not row.comment for row in rows), "native zip metadata is unsupported")
            require(sum(row.file_size for row in rows) <= 4 * LIMIT, "native zip expansion exceeds bounds")
            return {row.filename: held.read(row.filename) for row in rows}
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as held:
        expanded = held.read(4 * LIMIT + 1048577)
    require(len(expanded) <= 4 * LIMIT + 1048576, "native tar expansion exceeds bounds")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as held:
        rows = held.getmembers()
        require([row.name for row in rows] == names, "native tar has missing, extra, duplicate or reordered members")
        require(all(row.type == tarfile.REGTYPE and row.mode == 0o755 and row.uid == row.gid == row.mtime == 0 and row.uname == row.gname == "" and not row.pax_headers and not row.linkname and 0 < row.size <= LIMIT for row in rows), "native tar metadata is unsupported")
        require(sum(row.size for row in rows) <= 4 * LIMIT, "native tar expansion exceeds bounds")
        return {row.name: held.extractfile(row).read() for row in rows}


def unpack(body, names, selected):
    try:
        files = members(body, names, selected)
        require(archive.pack(list(files.items()), target(selected)["format"]) == body, "native archive representation is not deterministic packaging")
        return files
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, ValueError, RuntimeError) as failure:
        raise Refusal("native archive format is invalid or incomplete") from failure
