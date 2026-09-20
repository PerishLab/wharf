import json
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical, implementation, resources
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


def release(held):
    return bind.Release(held["repository"], held["marker"], held["commit"], held["tree"])


def artifact(held):
    return bind.Artifact(Path(held["dir"]), held["name"], held["target"])


def keyed(basis_held, modules, path):
    Path(path).write_bytes(canonical.encode(basis_held))
    return {"key": workload.key(basis_held, implementation.resourced(*modules))}


def key_binary(held):
    return keyed(basis.resolve(held["source"], held["name"], held["target"], held["runner"]), (["lib.cargo.basis", "lib.cargo.build"], []), held["basis"])


def key_suite(held):
    return keyed(basis.suite(held["source"], held["runner"]), (["lib.cargo.basis", "lib.cargo.suite"], []), held["basis"])


def key_bind(held):
    identity = {field: held[field] for field in RELEASE}
    return keyed({"entry": {"kind": "binary-identity", "binary": held["binary-key"]}, "identity": identity}, (["lib.identity.bind"], ["identity/format.json"]), held["basis"])


def key_smoke(held):
    return keyed({"entry": {"kind": "binary-smoke", "binary": held["binary-key"]}}, (["lib.identity.smoke"], []), held["basis"])


def targeted(target):
    for known in BUILD["targets"]:
        if known["target"] == target:
            return known
    raise Refusal(f"target {target} is not one this repository builds")


def resolved(entry, basis_held, modules):
    key = workload.key(basis_held, implementation.resourced(*modules))
    if key != entry["key"]:
        raise Refusal(f"this job resolved {key}, the plan for this run recorded {entry['key']}")
    return key


def run_binary(held):
    bucket = r2.configured()
    context = {field: held[field] for field in CONTEXT}
    target = targeted(held["target"])
    entry = plan.planned(plan.read(bucket, context), f"binary-{target['name']}")
    name = plan.product(context)
    held_basis = basis.resolve(held["source"], name, target["target"], target["runner"])
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.build"], []))
    output = Path(tempfile.mkdtemp())
    build.build(build.Build(Path(held["source"]), name, target["target"], output))
    return workload.publish(bucket, entry["key"], workload.Produced(output, held_basis, context))


def run_suite(held):
    return suite.suite(suite.Suite(Path(held["source"]), Path(held["output"])))


def run_bind(held):
    return bind.perform(artifact(held), release(held), held["binary-key"], held["output"])


def validate(held):
    return configured(artifact(held), held["repository"], held["marker"])


def run_smoke(held):
    return smoke(artifact(held), held["output"], held["expect"])


def key_node(held):
    return keyed(node.basis(held["source"], held["runner"]), (["lib.media.node"], []), held["basis"])


def node_engines(held):
    return node.declared(held["source"])


def node_suite(held):
    return node.suite(node.Suite(Path(held["source"]), Path(held["output"])))


def npm_plan(held):
    pending = npm.pending(held["source"], version.marker(held["marker"]))
    return {"published": all(item["published"] for item in pending), "packages": pending}


def npm_publish(held):
    return npm.publish(held["source"], version.marker(held["marker"]))


def oci_plan(held):
    image = oci.reference(held["repository"], version.marker(held["marker"]))
    return {"image": image, "published": oci.exists(image)}


def oci_publish(held):
    named = bind.Artifact(Path(held["dir"]), held["repository"].split("/", 1)[1], held["target"])
    image = oci.reference(held["repository"], version.marker(held["marker"]))
    return oci.publish(oci.Image(Path(held["source"]), named.file, named.name, image))


def chart_plan(held):
    pending = chart.pending(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))
    return {"published": all(item["published"] for item in pending), "charts": pending}


def chart_publish(held):
    return chart.publish(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))


def published_release(held):
    return releasing.Release(held["repository"], held["marker"], held["commit"], held["wharf"])


def release_plan(held):
    published = published_release(held)
    return {"channel": releasing.channel(published.marker), "published": releasing.published(published)}


def release_managers(held):
    published = published_release(held)
    binary = bind.Artifact(Path(held["dir"]), held["name"], "x86_64-unknown-linux-gnu").file
    return releasing.render(published, binary, held["source"], held["output"])


def release_publish(held):
    published = published_release(held)
    bound = {"x86_64-unknown-linux-gnu": held["linux"], "x86_64-pc-windows-msvc": held["windows"], "aarch64-apple-darwin": held["macos"]}
    return releasing.publish(published, releasing.Contents(bound, Path(held["managers"])), r2.writer(releasing.place(published)[1], "RELEASES"))


def key_cfworker(held):
    return keyed(cfworker.basis(held["source"], held["runner"]), (["lib.media.cfworker"], []), held["basis"])


def cfworker_deploy(held):
    return cfworker.deploy(cfworker.Deploy(Path(held["source"]), Path(held["output"])))


def cargo_plan(held):
    pending = publish.pending(held["source"], version.marker(held["marker"]))
    return {"published": all(item["published"] for item in pending), "packages": pending}


def cargo_publish(held):
    bound = version.inject(held["source"], version.marker(held["marker"]))
    return publish.publish(held["source"], bound["version"])


ACTIONS = {
    "key-binary": (key_binary, ["source", "name", "target", "runner", "basis"]),
    "key-suite": (key_suite, ["source", "runner", "basis"]),
    "key-bind": (key_bind, ["binary-key", *RELEASE, "basis"]),
    "key-smoke": (key_smoke, ["binary-key", "basis"]),
    "binary": (run_binary, ["source", "target", *CONTEXT]),
    "suite": (run_suite, ["source", "output"]),
    "bind": (run_bind, ["dir", "name", "target", *RELEASE, "binary-key", "output"]),
    "smoke": (run_smoke, ["dir", "name", "target", "output", "expect"]),
    "validate": (validate, ["dir", "name", "target", "repository", "marker"]),
    "key-node": (key_node, ["source", "runner", "basis"]),
    "node-engines": (node_engines, ["source"]),
    "node-suite": (node_suite, ["source", "output"]),
    "npm-plan": (npm_plan, ["source", "marker"]),
    "npm-publish": (npm_publish, ["source", "marker"]),
    "oci-plan": (oci_plan, ["repository", "marker"]),
    "oci-publish": (oci_publish, ["source", "dir", "target", "repository", "marker"]),
    "chart-plan": (chart_plan, ["source", "repository", "marker"]),
    "chart-publish": (chart_publish, ["source", "repository", "marker"]),
    "release-plan": (release_plan, ["repository", "marker", "commit", "wharf"]),
    "release-managers": (release_managers, ["repository", "marker", "commit", "wharf", "dir", "name", "source", "output"]),
    "release-publish": (release_publish, ["repository", "marker", "commit", "wharf", "linux", "windows", "macos", "managers"]),
    "key-cfworker": (key_cfworker, ["source", "runner", "basis"]),
    "cfworker-deploy": (cfworker_deploy, ["source", "output"]),
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
