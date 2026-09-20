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

RELEASE = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")
BUILD = resources.read_json("build.json")
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


def opened(held, name):
    bucket = r2.configured()
    document = plan.read(bucket, carried(held))
    return bucket, document, plan.planned(document, name)


def bound(bucket, document, name):
    return staged(bucket, document["entries"][f"bind-{name}"]["key"])


def place():
    return Path(tempfile.mkdtemp()) / "produced"


def staged(bucket, key):
    directory = place()
    workload.fetch(bucket, key, str(directory))
    return directory


def run_binary(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"binary-{target['name']}")
    name = plan.product(document["context"])
    held_basis = basis.resolve(held["source"], name, target["target"], target["runner"])
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.build"], []))
    output = place()
    build.build(build.Build(Path(held["source"]), name, target["target"], output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_suite(held):
    bucket, _, entry = opened(held, "suite-linux")
    held_basis = basis.suite(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.suite"], []))
    output = place()
    suite.suite(suite.Suite(Path(held["source"]), output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_bind(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"bind-{target['name']}")
    binary = document["entries"][f"binary-{target['name']}"]["key"]
    identity = {field: document["context"][field] for field in RELEASE}
    held_basis = {"entry": {"kind": "binary-identity", "binary": binary}, "identity": identity}
    resolved(entry, held_basis, (["lib.identity.bind"], ["identity/format.json"]))
    output = place()
    bound = bind.Artifact(staged(bucket, binary), plan.product(identity), target["target"])
    bind.perform(bound, bind.Release(**identity), binary, str(output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def validate(held):
    bucket, document, _ = opened(held, "release")
    target = targeted(BUILD["primary"])
    artifact = bind.Artifact(bound(bucket, document, target["name"]), plan.product(document["context"]), target["target"])
    return configured(artifact, held["repository"], held["marker"])


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


def decided(published, held):
    return dict(parameters.answer({"decision": "skip" if published else "run"}), **held)


def npm_plan(held):
    pending = npm.pending(held["source"], version.marker(held["marker"]))
    return decided(all(item["published"] for item in pending), {"packages": pending})


def npm_publish(held):
    return npm.publish(held["source"], version.marker(held["marker"]))


def oci_plan(held):
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
    return decided(all(item["published"] for item in pending), {"charts": pending})


def chart_publish(held):
    return chart.publish(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))


def published_release(held):
    return releasing.Release(held["repository"], held["marker"], held["commit"], held["wharf"])


def rendered(document, published, held, directory):
    name = plan.product(document["context"])
    binary = bind.Artifact(directory, name, targeted(BUILD["primary"])["target"]).file
    managers = place()
    releasing.render(published, binary, held["source"], str(managers))
    return managers


def release_plan(held):
    published = published_release(held)
    return decided(releasing.published(published), {"channel": releasing.channel(published.marker)})


def run_release(held):
    bucket, document, _ = opened(held, "release")
    context = document["context"]
    published = releasing.Release(context["repository"], context["marker"], context["commit"], held["wharf"])
    directories = {target["target"]: bound(bucket, document, target["name"]) for target in BUILD["targets"]}
    managers = rendered(document, published, held, directories[targeted(BUILD["primary"])["target"]])
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
    return decided(all(item["published"] for item in pending), {"packages": pending})


def cargo_publish(held):
    bound = version.inject(held["source"], version.marker(held["marker"]))
    return publish.publish(held["source"], bound["version"])


ACTIONS = {
    "binary": (run_binary, ["source", "target", *CONTEXT]),
    "suite": (run_suite, ["source", *CONTEXT]),
    "bind": (run_bind, ["target", *CONTEXT]),
    "smoke": (run_smoke, ["target", *CONTEXT]),
    "validate": (validate, [*CONTEXT]),
    "node-engines": (node_engines, ["source"]),
    "node-suite": (node_suite, ["source", *CONTEXT]),
    "npm-plan": (npm_plan, ["source", "marker"]),
    "npm-publish": (npm_publish, ["source", "marker"]),
    "oci-plan": (oci_plan, ["repository", "marker"]),
    "oci-publish": (oci_publish, ["source", *CONTEXT]),
    "chart-plan": (chart_plan, ["source", "repository", "marker"]),
    "chart-publish": (chart_publish, ["source", "repository", "marker"]),
    "release-plan": (release_plan, ["repository", "marker", "commit", "wharf"]),
    "release": (run_release, ["source", *CONTEXT]),
    "cfworker-deploy": (cfworker_deploy, ["source", *CONTEXT]),
    "cargo-plan": (cargo_plan, ["source", "marker"]),
    "cargo-publish": (cargo_publish, ["source", "marker"]),
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
