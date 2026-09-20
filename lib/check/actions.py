import re
from pathlib import Path

from lib.content import resources

LOCKED = resources.read_json("check/actions.json")
PINNED = LOCKED["pinned"]
RUNTIMES = LOCKED["runtimes"]
USES = re.compile(r"uses:\s*([^\s@]+)@(\S+)")


def check(root, paths):
    findings = []
    for path in [path for path in paths if path.suffix in (".yml", ".yaml")]:
        for number, line in enumerate((Path(root) / path).read_text().splitlines(), 1):
            match = USES.search(line)
            if not match:
                continue
            name, reference = match.groups()
            if name not in PINNED:
                findings.append(f"{path}:{number}: {name} is not in the pinned action list")
            elif reference != PINNED[name]["sha"]:
                findings.append(f"{path}:{number}: {name} must be pinned to {PINNED[name]['sha']}")
            elif PINNED[name].get("runtime") not in RUNTIMES:
                findings.append(f"{path}:{number}: {name} runs on {PINNED[name].get('runtime')}, allowed runtimes are {RUNTIMES}")
    return findings
