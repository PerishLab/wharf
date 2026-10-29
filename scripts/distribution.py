import json
import sys

from lib import parameters
from lib.media import release
from lib.refusal import Refusal
from lib.process.trial import admission, consumer, judgment, declaration
from lib.store import distribution, r2

IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "commit", "tree", "wharf", "run", "attempt")


def record(held):
    placed = release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])
    _, bucket, authority = release.place(placed)
    context = {field: held[field] for field in CONTEXT}
    trial = json.loads(held.get("trial", "null"))
    context["trial"] = None if trial is None else declaration.bound(trial, {field: context[field] for field in IDENTITY})
    result = distribution.record(r2.writer(bucket, "RELEASES"), context, json.loads(held["needs"]), lambda key: release.fetch(f"{authority}/{key}"))
    parameters.answer({"state": result["state"]})
    return result


ACTIONS = {
    "record": (record, [*CONTEXT, "needs", "trial"]),
    "trial-admit": (admission.admit, ["source", "run", "publisher", "selection"]),
    "trial-run": (consumer.execute, ["source", "trialcase"]),
    "trial-judge": (judgment.judge, ["source"]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("distribution", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"distribution: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
