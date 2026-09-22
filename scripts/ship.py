import json
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import implementation, resources
from lib.cargo import basis, build, publish, suite, version
from lib.identity import bind
from lib.refusal import Refusal
from lib.identity.smoke import configured, smoke
from lib.media import cfworker, chart, node, npm, oci, release as releasing
from lib.store import plan, r2
from lib.store import workload

DEPENDENCIES = "dependencies.tar.gz"
RELEASE = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")
PLANNED = (*CONTEXT, "planned")
BUILD = resources.read_json("build.json")
LAYERED = sorted({spec["action"] for spec in resources.read_json("units.json")["units"].values()})
RUNNER = BUILD["runner"]


def release(held):
    return bind.Release(held["repository"], held["marker"], held["commit"], held["tree"])


def targeted(target):
    for known in BUILD["targets"]:
        if target in (known["target"], known["name"]):
            return known
    raise Refusal(f"target {target} is not one this repository builds")


def resolved(entry, basis_held, modules):
    key = workload.key(basis_held, implementation.resourced(*modules))
    if key != entry["key"]:
        raise Refusal(f"this job resolved {key}, the plan for this run recorded {entry['key']}")
    return key


def carried(held):
    return {field: held[field] for field in CONTEXT}


def addressed(held):
    return dict(carried(held), attempt=held["planned"])


def opened(held, name):
    bucket = r2.configured()
    document = plan.read(bucket, addressed(held))
    return bucket, document, plan.planned(document, name)


def bound(bucket, document, name):
    return staged(bucket, document["entries"][f"bind-{name}"]["key"])


def place():
    return Path(tempfile.mkdtemp()) / "produced"


def staged(bucket, key):
    directory = place()
    workload.fetch(bucket, key, str(directory))
    return directory


def inherited(bucket, document, held, target):
    entry = document["entries"].get(f"dependencies-{target['name']}")
    if entry is None or entry["decision"] != "skip":
        return entry
    build.restore(held["source"], staged(bucket, entry["key"]) / DEPENDENCIES)
    return entry


def depended(bucket, held, target, entry):
    name = plan.product(carried(held))
    held_basis = basis.dependencies(held["source"], name, target["target"], target["runner"])
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.build"], []))
    output = place()
    output.mkdir(parents=True)
    build.archive(held["source"], output / DEPENDENCIES)
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_binary(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"binary-{target['name']}")
    name = plan.product(document["context"])
    held_basis = basis.resolve(held["source"], name, target["target"], target["runner"])
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.build"], []))
    dependencies = inherited(bucket, document, held, target)
    output = place()
    build.build(build.Build(Path(held["source"]), name, target["target"], output))
    if dependencies is not None and dependencies["decision"] == "run":
        print(json.dumps(depended(bucket, held, target, dependencies), sort_keys=True), file=sys.stderr)
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_suite(held):
    bucket, _, entry = opened(held, "suite-linux")
    held_basis = basis.suite(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.suite"], []))
    output = place()
    suite.suite(suite.Suite(Path(held["source"]), output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def binding(document):
    named = [target for target in BUILD["targets"] if document["entries"].get(f"bind-{target['name']}", {}).get("decision") == "run"]
    if not named:
        raise Refusal("the plan for this run decided no target needs binding")
    return named


def bind_target(bucket, document, held, target):
    entry = plan.planned(document, f"bind-{target['name']}")
    binary = document["entries"][f"binary-{target['name']}"]["key"]
    identity = {field: document["context"][field] for field in RELEASE}
    held_basis = {"entry": {"kind": "binary-identity", "binary": binary}, "identity": identity}
    resolved(entry, held_basis, (["lib.identity.bind"], ["identity/format.json"]))
    output = place()
    bound = bind.Artifact(staged(bucket, binary), plan.product(identity), target["target"])
    bind.perform(bound, bind.Release(**identity), binary, str(output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_bind(held):
    bucket = r2.configured()
    document = plan.read(bucket, addressed(held))
    return [bind_target(bucket, document, held, target) for target in binding(document)]


def validate(held):
    bucket, document, entry = opened(held, "validate")
    target = targeted(BUILD["primary"])
    primary = document["entries"][f"bind-{target['name']}"]["key"]
    held_basis = {"entry": {"kind": "binary-validate", "binary": primary}}
    resolved(entry, held_basis, (["lib.identity.smoke"], ["validators.json"]))
    artifact = bind.Artifact(staged(bucket, primary), plan.product(document["context"]), target["target"])
    output = place()
    configured(artifact, str(output), {"repository": held["repository"], "marker": held["marker"]})
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_smoke(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"smoke-{target['name']}")
    bound = document["entries"][f"bind-{target['name']}"]["key"]
    held_basis = {"entry": {"kind": "binary-smoke", "binary": bound}}
    resolved(entry, held_basis, (["lib.identity.smoke"], []))
    name = plan.product(document["context"])
    output = place()
    artifact = bind.Artifact(staged(bucket, bound), name, target["target"])
    smoke(artifact, str(output), f"{name} {document['context']['marker']}")
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def node_engines(held):
    return parameters.answer(node.declared(held["source"]) if node.carried(held["source"]) else {})


def node_suite(held):
    bucket, _, entry = opened(held, "suite-node")
    held_basis = node.basis(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.media.node"], []))
    output = place()
    node.suite(node.Suite(Path(held["source"]), output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def decided(published, held, carried=True):
    return dict(parameters.answer({"decision": "skip" if published else "run", "presence": "present" if carried else "none"}), **held)


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
    return decided(oci.exists(image), {"image": image})


def oci_publish(held):
    bucket, document, _ = opened(held, "oci")
    target = targeted(BUILD["primary"])
    artifact = bind.Artifact(bound(bucket, document, target["name"]), plan.product(document["context"]), target["target"])
    image = oci.reference(held["repository"], version.marker(held["marker"]))
    return oci.publish(oci.Image(Path(held["source"]), artifact.file, artifact.name, image))


def chart_plan(held):
    pending = chart.pending(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))
    return decided(all(item["published"] for item in pending), {"charts": pending}, bool(pending))


def chart_publish(held):
    return chart.publish(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))


def published_release(held):
    return releasing.Release(held["repository"], held["marker"], held["commit"], held["wharf"])


def release_plan(held):
    published = published_release(held)
    return decided(releasing.published(published), {"channel": releasing.channel(published.marker)})


def run_release(held):
    bucket, document, _ = opened(held, "release")
    context = document["context"]
    published = releasing.Release(context["repository"], context["marker"], context["commit"], held["wharf"])
    directories = {target["target"]: bound(bucket, document, target["name"]) for target in BUILD["targets"]}
    managers = place()
    releasing.render(published, str(managers))
    return releasing.publish(published, releasing.Contents(directories, managers), r2.writer(releasing.place(published)[1], "RELEASES"))


def cfworker_deploy(held):
    bucket, _, entry = opened(held, "cfworker")
    held_basis = cfworker.basis(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.media.cfworker"], []))
    output = place()
    cfworker.deploy(cfworker.Deploy(Path(held["source"]), output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def cargo_plan(held):
    pending = publish.pending(held["source"], version.marker(held["marker"]))
    return decided(all(item["published"] for item in pending), {"packages": pending}, bool(pending))


def cargo_publish(held):
    bound = version.inject(held["source"], version.marker(held["marker"]))
    return publish.publish(held["source"], bound["version"])


def step(held):
    if held["unit"] not in LAYERED:
        raise Refusal(f"a layer runs one of {', '.join(LAYERED)}, not {held['unit']}")
    handler, names = ACTIONS[held["unit"]]
    values, origins = parameters.resolve(held["unit"], names, [])
    print("\n".join(parameters.report(values, origins)), file=sys.stderr)
    return handler(values)


ACTIONS = {
    "binary": (run_binary, ["source", "target", *PLANNED]),
    "suite": (run_suite, ["source", *PLANNED]),
    "bind": (run_bind, [*PLANNED]),
    "smoke": (run_smoke, ["target", *PLANNED]),
    "validate": (validate, [*PLANNED]),
    "node-engines": (node_engines, ["source"]),
    "node-suite": (node_suite, ["source", *PLANNED]),
    "npm-plan": (npm_plan, ["source", "marker"]),
    "npm-publish": (npm_publish, ["source", "marker"]),
    "oci-plan": (oci_plan, ["source", "repository", "marker"]),
    "oci-publish": (oci_publish, ["source", *PLANNED]),
    "chart-plan": (chart_plan, ["source", "repository", "marker"]),
    "chart-publish": (chart_publish, ["source", "repository", "marker"]),
    "release-plan": (release_plan, ["repository", "marker", "commit", "wharf"]),
    "release": (run_release, ["source", *PLANNED]),
    "cfworker-deploy": (cfworker_deploy, ["source", *PLANNED]),
    "cargo-plan": (cargo_plan, ["source", "marker"]),
    "cargo-publish": (cargo_publish, ["source", "marker"]),
    "step": (step, ["unit"]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("ship", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"ship: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
