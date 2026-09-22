import io
import subprocess
import tarfile

from lib.media import depot, edit
from lib.refusal import Refusal


def changelog(source, marker, directory):
    result = subprocess.run(["plumb", "changelog", str(source), "--version", marker, "--prove", str(directory)], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refusal(f"plumb changelog refused the consigned notes for {marker}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def carried(source, marker, product):
    root = f"skills/{product}"
    result = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", f"refs/tags/{marker}", root], capture_output=True)
    if result.returncode != 0:
        raise Refusal(f"{marker} carries no {root}: {result.stderr.decode(errors='replace').strip()}")
    held = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            if member.isfile():
                relative = member.name.removeprefix(f"{root}/")
                held[relative] = (depot.sha(archive.extractfile(member).read()), bool(member.mode & 0o111))
    return held


def skill(source, marker, product, directory):
    expected = carried(source, marker, product)
    found = {relative: (entry["sha256"], entry["executable"]) for relative, (entry, _) in edit.directory(directory).items()}
    if found != expected:
        drifted = sorted(set(found.items()) ^ set(expected.items()))
        raise Refusal(f"the consigned skill differs from skills/{product} at {marker}: {', '.join(path for path, _ in drifted)}")
    return f"skill matches skills/{product} at {marker}"

