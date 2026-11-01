import shutil
import subprocess
import tempfile
from pathlib import Path

from lib.process.association.spec import LIMIT, POLICY, require, sha
from lib.refusal import Refusal


def executable():
    path = shutil.which(POLICY["signer"]["name"])
    require(path is not None, "native association requires the prepared pinned signer")
    path = Path(path).resolve()
    require(path.is_file() and sha(path.read_bytes()) == POLICY["signer"]["sha256"], "native association signer bytes differ from fixed policy")
    return path


def sign(body, name):
    require(isinstance(body, bytes) and 0 < len(body) <= LIMIT and "/" not in name and "\\" not in name and name not in ("", ".", ".."), "signature material is outside bounds")
    tool = executable()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / name
        path.write_bytes(body)
        try:
            subprocess.run([str(tool), "sign", str(path)], cwd=directory, env={"HOME": directory, "LANG": "C"}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=POLICY["signer"]["seconds"], check=True)
        except (OSError, subprocess.SubprocessError) as failure:
            raise Refusal("pinned signature transformation failed") from failure
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= LIMIT, "signature output is outside bounds")
        result = path.read_bytes()
    require(executable() == tool, "native association signer changed during execution")
    return result
