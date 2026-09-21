import json
import sys

from lib import parameters
from lib.refusal import Refusal
from lib.store import plan, r2, trigger

CONTEXT = ("repository", "marker", "wharf", "run", "attempt", "actor")
IDENTITY = ("commit", "tree")


def identified(bucket, context, planned):
    held = plan.standing_for(bucket, dict(context, attempt=planned))
    return {field: held["context"][field] if held else None for field in IDENTITY}


def record(held):
    bucket = r2.configured()
    context = {field: held[field] for field in CONTEXT}
    return trigger.record(bucket, dict(context, **identified(bucket, context, held["planned"])), json.loads(held["needs"]))


ACTIONS = {"trigger": (record, [*CONTEXT, "planned", "needs"])}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("store", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"store: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
