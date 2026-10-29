from pathlib import Path
import re

from lib.process import git


def workflow():
    needs, current = {}, None
    for line in (Path(git(".", "rev-parse", "--show-toplevel")) / ".github/workflows/ship.yml").read_text().splitlines():
        if re.fullmatch(r"  [a-z0-9-]+:", line):
            current = line.strip().rstrip(":")
            needs[current] = []
        elif current and line.startswith("    needs:"):
            needs[current] = re.findall(r"[a-z0-9-]+", line.split(":", 1)[1])
    return needs
