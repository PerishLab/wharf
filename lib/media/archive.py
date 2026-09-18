import gzip
import io
import tarfile
import zipfile

MODE = 0o755
EPOCH = (1980, 1, 1, 0, 0, 0)


def tarball(name, body):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        entry = tarfile.TarInfo(name)
        entry.size, entry.mode, entry.mtime = len(body), MODE, 0
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = ""
        archive.addfile(entry, io.BytesIO(body))
    return gzip.compress(buffer.getvalue(), mtime=0)


def zipball(name, body):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo(name, EPOCH)
        entry.external_attr = (0o100000 | MODE) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, body)
    return buffer.getvalue()


BUILDERS = {"tar.gz": tarball, "zip": zipball}


def pack(member, body, fmt):
    return BUILDERS[fmt](member, body)
