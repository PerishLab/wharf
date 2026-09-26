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
    brief = directory / BRIEF
    if not brief.is_file():
        raise Refusal(f"the consigned skill carries no {BRIEF}")
    size = brief.stat().st_size
    if size > CAP:
        raise Refusal(f"the consigned {BRIEF} carries {size} bytes where plumb's rule://seat/wayfinder caps {CAP}")
    return f"skill carries a {BRIEF} of {size} bytes"
