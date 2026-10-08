import json
import re
import sys
from pathlib import Path

from lib import parameters
from lib.content import canonical, preview
from lib.content.static import admission, workspace
from lib.refusal import Refusal

NAMED = ("repository", "commit", "tree")
SOURCED = NAMED + ("source",)
REQUESTED = ("request",)


def named(held):
    preview.matches(held["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "acquisition repository")
    for key in ("commit", "tree"):
        preview.matches(held[key], preview.HEX[40], "acquisition " + key)
    owner, name = held["repository"].split("/")
    return parameters.answer({"owner": owner, "name": name, "commit": held["commit"], "tree": held["tree"]})


def source(held):
    named({key: held[key] for key in NAMED})
    return workspace.acquired(Path(held["source"]).resolve(), {key: held[key] for key in NAMED})


def request(held):
    intent = admission.requested(held["request"])
    admission.context()
    owner, name = intent["repository"].split("/")
    return parameters.answer({"owner": owner, "name": name, "repository": intent["repository"], "operation": intent["operation"],
                              "intent": canonical.digest(intent)})


def admit(held):
    intent = admission.requested(held["request"])
    observation = admission.observe(intent)
    return parameters.answer({"intent": canonical.digest(intent), "admission": canonical.digest(observation)})


ACTIONS = {"named": named, "source": source, "request": request, "admit": admit}


def main(argv=None):
    try:
        action, rest = parameters.acted("preview", ACTIONS, sys.argv[1:] if argv is None else argv)
        values, origins = parameters.resolve(action, {"named": NAMED, "source": SOURCED, "request": REQUESTED, "admit": REQUESTED}[action], rest)
        result = ACTIONS[action](values)
    except Refusal as error:
        print(f"preview: refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
