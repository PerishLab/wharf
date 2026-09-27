import gzip
import io
import tarfile
import zipfile
from pathlib import Path

from lib.refusal import Refusal

MODE = 0o755
EPOCH = (1980, 1, 1, 0, 0, 0)


def tarball(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, body in members:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(body), MODE, 0
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            archive.addfile(entry, io.BytesIO(body))
    return gzip.compress(buffer.getvalue(), mtime=0)


def zipball(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            entry = zipfile.ZipInfo(name, EPOCH)
            entry.external_attr = (0o100000 | MODE) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, body)
    return buffer.getvalue()


BUILDERS = {"tar.gz": tarball, "zip": zipball}


def members(directory, target, names):
    suffix = ".exe" if "windows" in target else ""
    held = []
    for name in names:
        source = Path(directory) / f"{name}-{target}{suffix}"
        if not source.is_file():
            raise Refusal(f"{source} is missing")
        held.append((f"{name}{suffix}", source.read_bytes()))
    return held


def pack(held, fmt):
    names = [name for name, _ in held]
    if not names or len(set(names)) != len(names):
        raise Refusal(f"an archive holds one or more distinct members, not {names}")
    return BUILDERS[fmt](held)
