import json
import re
import sys
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.content.lane import preview
from lib.content.static import admission, workspace
from lib.cargo import toolchain
from lib.process.lane import build, deployment, disposal, entry, provider, reservation, transfer
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
    return workspace.acquired(Path(held["source"]), {key: held[key] for key in NAMED})


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


def lane_request(held):
    intent = entry.requested(held["request"])
    entry.platform()
    owner, name = intent["target"]["repository"].split("/")
    origin = intent["source"] or {}
    return parameters.answer({"owner": owner, "name": name, "repository": intent["target"]["repository"], "operation": intent["operation"],
                              "intent": canonical.digest(intent), "commit": origin.get("commit", ""), "tree": origin.get("tree", "")})


def lane_admit(held):
    intent = entry.requested(held["request"])
    observation = entry.observe(intent)
    return parameters.answer({"intent": canonical.digest(intent), "admission": canonical.digest(observation), "observed": canonical.encode(observation).decode()})


def lane_engines(held):
    versions = toolchain.resolve()
    return parameters.answer({"domain": toolchain.encoded(versions), "node": versions["node.version"], "pnpm": versions["pnpm.version"]})


def lane_build(held):
    result = build.record(held)
    return parameters.answer(dict(result, artifact=json.dumps(result["artifact"], separators=(",", ":"))))


def lane_reserve(held):
    result = reservation.prepare(held)
    return parameters.answer({"prepared": canonical.encode(result).decode()})


def lane_consume(held):
    result = transfer.prepare(held)
    return parameters.answer({"consumed": canonical.encode(result).decode()})


def lane_observe(held):
    result = provider.observe(held)
    return parameters.answer({"provider": canonical.encode(result).decode()})


def lane_dispose(held):
    result = disposal.perform(held)
    return parameters.answer({"result": canonical.encode(result).decode()})


def lane_apply(held):
    result = deployment.perform(held)
    return parameters.answer({"result": canonical.encode(result).decode()})


ACTIONS = {"named": named, "source": source, "request": request, "admit": admit,
           "lane-request": lane_request, "lane-admit": lane_admit, "lane-engines": lane_engines, "lane-build": lane_build,
           "lane-reserve": lane_reserve, "lane-consume": lane_consume, "lane-observe": lane_observe, "lane-dispose": lane_dispose, "lane-apply": lane_apply}
NAMES = {"named": NAMED, "source": SOURCED, "request": REQUESTED, "admit": REQUESTED,
         "lane-request": REQUESTED, "lane-admit": REQUESTED, "lane-engines": (), "lane-build": (*REQUESTED, "source", "admitted"),
         "lane-reserve": (*REQUESTED, "artifact"), "lane-consume": (*REQUESTED, "prepared", "workload"),
         "lane-observe": (*REQUESTED, "prepared", "workload"), "lane-dispose": (*REQUESTED, "prepared", "provider"),
         "lane-apply": (*REQUESTED, "prepared", "provider", "workload")}


def main(argv=None):
    try:
        action, rest = parameters.acted("preview", ACTIONS, sys.argv[1:] if argv is None else argv)
        values, origins = parameters.resolve(action, NAMES[action], rest)
        result = ACTIONS[action](values)
    except Refusal as error:
        print(f"preview: refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
