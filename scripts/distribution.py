import json
import sys

from lib import parameters
from lib.media import release
from lib.identity.bind import producer, comparison, candidate
from lib.refusal import Refusal
from lib.process.trial import admission, consumer, judgment, declaration
from lib.process.trial import credential, transport
from lib.process.promotion import operation as promotion
from lib.process.promotion import preflight, prepared
from lib.process.promotion import capture
from lib.process.confirmation import world, retention
from lib.process.confirmation import semantics
from lib.process.promotion import selection
from lib.store import distribution, plan, r2
from lib.process.association import producer as native, public as native_public, candidate as native_candidate

IDENTITY = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "commit", "tree", "wharf", "run", "attempt")


def record(held):
    placed = release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])
    _, bucket, authority = release.place(placed)
    context = {field: held[field] for field in CONTEXT}
    trial = json.loads(held.get("trial", "null"))
    context["trial"] = None if trial is None else declaration.bound(trial, {field: context[field] for field in IDENTITY})
    needs = json.loads(held["needs"])
    if needs.get("plan", {}).get("result") == "success":
        outputs = needs["plan"].get("outputs", {})
        try:
            reference = json.loads(outputs.get("plan-reference", ""))
        except (TypeError, ValueError):
            raise Refusal("successful planner has no valid immutable plan reference") from None
        query = plan.provenance(context, reference)
        if query["attempt"] != outputs.get("planned"):
            raise Refusal("distribution reference differs from the actual planner attempt")
        plan.referenced(r2.configured(), context, reference)
    publisher = r2.writer(bucket, "RELEASES")
    print(json.dumps(r2.reference(publisher), sort_keys=True), file=sys.stderr, flush=True)
    result = distribution.record(publisher, context, needs, lambda key: release.fetch(f"{authority}/{key}"))
    parameters.answer({"state": result["state"]})
    return result


ACTIONS = {
    "semantics-select-prepare": (selection.prepare, ["observer", "repository", "marker", "source"]),
    "semantics-select": (selection.produce, ["observer", "repository", "marker", "source"]),
    "semantics-select-confirm": (selection.confirm, ["observer", "repository", "marker", "source", "digest"]),
    "semantics-history": (semantics.checkpoint, ["observer", "repository", "marker", "source"]),
    "semantics-history-confirm": (semantics.confirmed, ["observer", "repository", "marker", "source", "digest"]),
    "semantics-retain": (retention.produce, ["observer", "repository", "marker", "artifact"]),
    "semantics-retain-confirm": (retention.confirm, ["observer", "repository", "marker", "artifact", "digest"]),
    "semantics-source": (world.source, ["observer", "repository", "marker", "artifact"]),
    "semantics-world": (world.produce, ["observer", "repository", "marker", "source", "artifact"]),
    "semantics-world-confirm": (world.confirm, ["observer", "repository", "marker", "artifact", "observation"]),
    "semantics-capture": (capture.produce, ["observer", "repository", "marker", "source"]),
    "semantics-transition": (semantics.transition, ["observer", "repository", "marker", "source"]),
    "semantics-issue": (semantics.issue, ["observer", "repository", "marker", "source"]),
    "semantics-confirm": (semantics.confirm, ["observer", "repository", "marker", "source", "digest"]),
    "request": (preflight.request, ["observer", "selection"]),
    "prepared": (prepared.produce, ["observer", "repository", "marker", "run", "planned", "preparation", "source"]),
    "prepared-read": (prepared.consume, ["observer", "repository", "marker", "run", "planned", "preparation", "source", "digest"]),
    "promotion-prepare": (promotion.prepare, ["repository", "marker", "run", "planned", "observer", "source"]),
    "promotion-read": (promotion.read, ["repository", "marker", "run", "planned", "observer", "source", "digest"]),
    "record": (record, [*CONTEXT, "needs", "trial"]),
    "trial-admit": (admission.admit, ["source", "run", "publisher", "selection"]),
    "trial-run": (consumer.execute, ["source", "trialcase"]),
    "trial-judge": (judgment.judge, ["source"]),
    "windtun-prepare": (transport.prepare, ["source", "observer", "trialcase"]),
    "windtun-load": (transport.load, ["source", "observer", "repository", "marker"]),
    "windtun-store": (transport.store, ["source", "observer", "repository", "marker"]),
    "windtun-issue": (credential.issue, ["observer", "repository", "marker", "digest"]),
    "native-proof": (native.produce, ["observer", "repository", "marker", "run", "observation"]),
    "native-read": (native_public.check, ["repository", "marker", "observation"]),
    "native-compare": (native_public.compare, ["repository", "marker", "reference", "source", "observation"]),
    "native-candidate": (native_candidate.produce, ["observer", "repository", "marker", "run", "attempt", "reference", "source"]),
    "native-candidate-read": (native_candidate.consume, ["observer", "digest", "source"]),
    "build-proof": (producer.produce, ["observer", "repository", "marker", "run"]),
    "build-compare": (comparison.run, ["repository", "marker", "reference", "source"]),
    "build-candidate": (candidate.produce, ["observer", "repository", "marker", "run", "attempt", "reference", "source"]),
    "build-candidate-read": (candidate.consume, ["observer", "digest", "source"]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    action, values = None, {}
    try:
        action, rest = parameters.acted("distribution", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
        if action in ("native-proof", "native-read", "native-compare"):
            native_public.observe(action, values, result)
    except Refusal as refusal:
        if action in ("native-proof", "native-read", "native-compare"):
            try:
                native_public.observe(action, values, refusal)
            except Refusal:
                print("distribution: native observation unavailable", file=sys.stderr)
        state = "unavailable" if isinstance(refusal, native_public.Unavailable) else "refused"
        print(f"distribution: {state}: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    if action == "trial-run" and result.get("exit", 0) != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
