import json
import sys

from lib import parameters
from lib.process.follow import event, operation
from lib.refusal import Refusal


def qualify(held):
    summary = event.qualified(held["request"])
    owner, name = summary["repository"].split("/")
    return dict(parameters.answer({"repository": summary["repository"], "owner": owner, "name": name}), delivery=summary["delivery"])


def execute(held):
    return operation.execute(held["request"])


ACTIONS = {"qualify": qualify, "execute": execute}


def main(argv=None):
    try:
        action, rest = parameters.acted("follow", ACTIONS, sys.argv[1:] if argv is None else argv)
        values, origins = parameters.resolve(action, ["request"], rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = ACTIONS[action](values)
    except Refusal as refusal:
        print(f"follow: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
