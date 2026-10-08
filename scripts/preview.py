import json
import re
import sys
from pathlib import Path

from lib import parameters
from lib.content import preview
from lib.content.static import workspace
from lib.refusal import Refusal

NAMED = ("repository", "commit", "tree")
SOURCED = NAMED + ("source",)


def named(held):
    preview.matches(held["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "acquisition repository")
    for key in ("commit", "tree"):
        preview.matches(held[key], preview.HEX[40], "acquisition " + key)
    owner, name = held["repository"].split("/")
    return parameters.answer({"owner": owner, "name": name, "commit": held["commit"], "tree": held["tree"]})


def source(held):
    named({key: held[key] for key in NAMED})
    return workspace.acquired(Path(held["source"]).resolve(), {key: held[key] for key in NAMED})


ACTIONS = {"named": named, "source": source}


def main(argv=None):
    try:
        action, rest = parameters.acted("preview", ACTIONS, sys.argv[1:] if argv is None else argv)
        values, origins = parameters.resolve(action, {"named": NAMED, "source": SOURCED}[action], rest)
        result = ACTIONS[action](values)
    except Refusal as error:
        print(f"preview: refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
