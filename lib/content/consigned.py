import subprocess

from lib.refusal import Refusal


def changelog(source, marker, directory):
    result = subprocess.run(["plumb", "changelog", str(source), "--version", marker, "--prove", str(directory)], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refusal(f"plumb changelog refused the consigned notes for {marker}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


BRIEF = "SKILL.md"
CAP = 3072


def skill(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal("the consigned skill is not a plain directory")
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise Refusal(f"cannot read the consigned skill: {error}") from error
    brief = directory / BRIEF
    if brief not in entries:
        raise Refusal(f"the consigned skill carries no {BRIEF}")
    if brief.is_symlink() or not brief.is_file():
        raise Refusal(f"the consigned {BRIEF} is not a regular file")
    if entries != [brief]:
        raise Refusal(f"the consigned skill carries entries beside {BRIEF}; a skill generation contains exactly one regular {BRIEF}")
    size = brief.stat().st_size
    if size > CAP:
        raise Refusal(f"the consigned {BRIEF} carries {size} bytes where plumb's rule://seat/wayfinder caps {CAP}")
    return f"skill carries a {BRIEF} of {size} bytes"


def scene(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal("the consigned scene is not a plain directory")
    files = 0
    for path in directory.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Refusal("the consigned scene carries a special object")
        if path.is_file():
            files += 1
    if not files:
        raise Refusal("the consigned scene holds no files")
    return f"scene carries {files} regular files; execution semantics belong to Seatrial"
