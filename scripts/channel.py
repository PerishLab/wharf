import json
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.media import release
from lib.refusal import Refusal
from lib.store import r2


def released(held):
    return release.Release(held["repository"], held["marker"], "", held.get("wharf", ""))


def plan(held):
    overtaken = release.overtaken(released(held))
    answered = {"decision": "skip", "presence": "overtaken"} if overtaken else {"decision": "run", "presence": "present"}
    return dict(parameters.answer(answered), channel=release.channel(held["marker"]))


def point(held):
    pointed = released(held)
    managers = Path(tempfile.mkdtemp()) / "managers"
    release.render(pointed, str(managers))
    return release.point(pointed, managers, r2.writer(release.place(pointed)[1], "RELEASES"))


ACTIONS = {"plan": (plan, ["repository", "marker"]), "point": (point, ["repository", "marker", "wharf"])}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("channel", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"channel: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
