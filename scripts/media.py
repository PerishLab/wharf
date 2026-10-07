import json
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.cargo import publish, toolchain, version
from lib.content import executables, resources
from lib.identity import bind
from lib.media import chart, node, npm, oci, release
from lib.refusal import Refusal
from lib.store import plan, r2, workload

RELEASE = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")
PLANNED = (*CONTEXT, "planned")
BUILD = resources.read_json("build.json")


def identified(held):
    return bind.Release(held["repository"], held["marker"], held["commit"], held["tree"])


def decided(published, held, carried=True):
    return dict(parameters.answer({"decision": "skip" if published else "run", "presence": "present" if carried else "none"}), **held)


def engines(held):
    versions = toolchain.resolve()
    answered = {"domain": toolchain.encoded(versions)}
    if node.carried(held["source"]):
        answered.update(node=versions["node.version"], pnpm=versions["pnpm.version"])
    return parameters.answer(answered)


def npm_plan(held):
    if not npm.carried(held["source"]):
        return decided(True, {"packages": []}, False)
    pending = npm.pending(held["source"], version.marker(held["marker"]))
    return decided(all(item["published"] for item in pending), {"packages": pending}, bool(pending))


def npm_publish(held):
    return npm.publish(held["source"], version.marker(held["marker"]))


def oci_plan(held):
    if not oci.carried(held["source"]):
        return decided(True, {"image": None}, False)
    image = oci.reference(held["repository"], version.marker(held["marker"]))
    if not executables.declared(held["source"], release.targets(held["source"])):
        return decided(False, {"image": image})
    return decided(oci.exists(image) and oci.current(image), {"image": image})


def oci_publish(held):
    bucket = r2.configured()
    document = plan.read(bucket, dict({field: held[field] for field in CONTEXT}, attempt=held["planned"]))
    plan.planned(document, "oci")
    listed = executables.declared(held["source"], release.targets(held["source"]))
    image = oci.reference(held["repository"], version.marker(held["marker"]))
    if not listed:
        return dict(oci.publish_source(oci.SourceImage(Path(held["source"]), image)), channel=oci.advance(image))
    primary = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])
    binary = executables.placed(held["source"], "oci", listed, plan.product(document["context"]))
    if binary not in executables.built(listed, primary["target"]):
        raise Refusal(f"[release.oci] carries {binary}, which is not built for {primary['target']}")
    directory = Path(tempfile.mkdtemp()) / "bound"
    workload.fetch(bucket, document["entries"][f"bind-{primary['name']}"]["key"], str(directory))
    artifact = bind.Artifact(directory, binary, primary["target"])
    return dict(oci.publish(oci.Image(Path(held["source"]), artifact.file, binary, image, held["marker"])), channel=oci.advance(image))


def release_plan(held):
    published = release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])
    done, carried = release.presence(held["source"], published)
    return decided(done, {"channel": release.channel(held["marker"])}, carried)


def chart_plan(held):
    pending = chart.pending(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))
    return decided(all(item["published"] for item in pending), {"charts": pending}, bool(pending))


def chart_publish(held):
    return chart.publish(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))


def cargo_plan(held):
    pending = publish.proven(held["source"], identified(held))
    return decided(all(item["published"] for item in pending), {"packages": pending}, bool(pending))


def cargo_publish(held):
    bound = version.inject(held["source"], identified(held))
    return publish.publish(held["source"], bound["version"])


ACTIONS = {
    "engines": (engines, ["source"]),
    "npm-plan": (npm_plan, ["source", "marker"]),
    "npm-publish": (npm_publish, ["source", "marker"]),
    "oci-plan": (oci_plan, ["source", "repository", "marker"]),
    "oci-publish": (oci_publish, ["source", *PLANNED]),
    "release-plan": (release_plan, ["repository", "marker", "commit", "wharf", "source"]),
    "chart-plan": (chart_plan, ["source", "repository", "marker"]),
    "chart-publish": (chart_publish, ["source", "repository", "marker"]),
    "cargo-plan": (cargo_plan, ["source", *RELEASE]),
    "cargo-publish": (cargo_publish, ["source", *RELEASE]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("media", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"media: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
