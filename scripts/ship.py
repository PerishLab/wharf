import json
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import executables, implementation, resources
from lib.cargo import basis, build, suite
from lib.debian import package, verify
from lib.debian.version import debian
from lib.identity import bind, guard
from lib.refusal import Refusal
from lib.identity.smoke import configured, smoke
from lib.media import cfworker, node, release as releasing
from lib.identity.guard import execution
from lib.store import plan, r2, workload

DEPENDENCIES = "dependencies.tar.gz"
RELEASE = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")
PLANNED = (*CONTEXT, "planned")
BUILD = resources.read_json("build.json")
LAYERED = sorted({spec["action"] for spec in resources.read_json("units.json")["units"].values()})
RUNNER = BUILD["runner"]
PRIMARY = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])
CARGO = (["lib.cargo.basis", "lib.cargo.build"], ["build.json", "identity/format.json"])
DEBIAN = (["lib.debian.package"], ["releases.json"])
VERIFYING = (["lib.debian.verify"], ["build.json"])


def declared(source):
    return executables.declared(source, releasing.targets(source))


def built(held, target):
    return executables.built(declared(held["source"]), target["target"])


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
    held_basis = basis.dependencies(held["source"], built(held, target), target["target"], target["runner"])
    resolved(entry, held_basis, CARGO)
    output = place()
    output.mkdir(parents=True)
    build.archive(held["source"], output / DEPENDENCIES)
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_binary(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"binary-{target['name']}")
    names = built(held, target)
    held_basis = basis.resolve(held["source"], names, target["target"], target["runner"])
    resolved(entry, held_basis, CARGO)
    dependencies = inherited(bucket, document, held, target)
    output = place()
    build.build(build.Build(Path(held["source"]), plan.product(document["context"]), tuple(names), target["target"], output))
    if dependencies is not None and dependencies["decision"] == "run":
        print(json.dumps(depended(bucket, held, target, dependencies), sort_keys=True), file=sys.stderr)
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_suite(held):
    bucket, _, entry = opened(held, "suite-linux")
    held_basis = basis.suite(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.cargo.basis", "lib.cargo.suite"], ["build.json"]))
    output = place()
    suite.suite(suite.Suite(Path(held["source"]), output))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


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
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_bind(held):
    bucket = r2.configured()
    document = plan.read(bucket, addressed(held))
    return [bind_target(bucket, document, held, target) for target in binding(document)]


def validate(held):
    bucket, document, entry = opened(held, "validate")
    primary = document["entries"][f"bind-{PRIMARY['name']}"]["key"]
    held_basis = {"entry": {"kind": "binary-validate", "binary": primary}}
    resolved(entry, held_basis, (["lib.identity.smoke"], ["validators.json"]))
    name = executables.primary(declared(held["source"]), plan.product(document["context"]))
    artifact = bind.Artifact(staged(bucket, primary), name, PRIMARY["target"])
    output = place()
    configured(artifact, str(output), {"repository": held["repository"], "marker": held["marker"]})
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_smoke(held):
    target = targeted(held["target"])
    bucket, document, entry = opened(held, f"smoke-{target['name']}")
    bound = document["entries"][f"bind-{target['name']}"]["key"]
    held_basis = {"entry": {"kind": "binary-smoke", "binary": bound}}
    resolved(entry, held_basis, (["lib.identity.smoke"], []))
    output = place()
    directory = staged(bucket, bound)
    artifacts = [bind.Artifact(directory, name, target["target"]) for name in bind.executables(directory, target["target"])]
    smoke(artifacts, str(output), document["context"]["marker"])
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def node_suite(held):
    bucket, _, entry = opened(held, "suite-node")
    held_basis = node.basis(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.media.node"], []))
    output = place()
    node.suite(node.Suite(Path(held["source"]), output))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def debianized(source, product, required=True):
    listed = declared(source)
    binary = executables.placed(source, "deb", listed, product)
    if binary is None and required:
        raise Refusal("the plan holds a deb the product does not declare")
    if binary is None:
        return None
    if binary not in executables.built(listed, PRIMARY["target"]):
        raise Refusal(f"[release.deb] carries {binary}, which is not built for {PRIMARY['target']}")
    return package.declared(source, binary)


def run_deb(held):
    bucket, document, entry = opened(held, "deb")
    context = document["context"]
    product = plan.product(context)
    held_deb = debianized(held["source"], product)
    binary = document["entries"][f"bind-{PRIMARY['name']}"]["key"]
    held_basis = package.basis(held["source"], held_deb, binary, debian(context["marker"]))
    resolved(entry, held_basis, DEBIAN)
    executable = bind.Artifact(staged(bucket, binary), held_deb.binary, PRIMARY["target"]).file
    output = place()
    package.build(package.Package(Path(held["source"]), held_deb, executable, debian(context["marker"]), output))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def run_verify(held):
    bucket, document, entry = opened(held, "verify-deb")
    context = document["context"]
    product = plan.product(context)
    held_deb = debianized(held["source"], product)
    deb = document["entries"]["deb"]["key"]
    held_basis = {"entry": {"kind": "deb-verify", "deb": deb}}
    resolved(entry, held_basis, VERIFYING)
    depends, units = tuple(package.depends(held["source"], held_deb)), package.units(held["source"], held_deb)
    check = verify.Check(staged(bucket, deb) / package.named(held_deb.binary, debian(context["marker"])), held_deb.binary, debian(context["marker"]), context["marker"], depends, units)
    output = place()
    verify.verify(check, str(output))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def placements(bucket, document, source):
    context = document["context"]
    held_deb = debianized(source, plan.product(context), False)
    if held_deb is None:
        return {}
    entries = document["entries"]
    if "deb" not in entries or "verify-deb" not in entries:
        raise Refusal("plumb.toml declares a deb this run's plan did not build and verify")
    if not workload.reusable(bucket, entries["verify-deb"]["key"]):
        raise Refusal("the deb this release carries has no recorded verification")
    return {"deb": staged(bucket, entries["deb"]["key"]) / package.named(held_deb.binary, debian(context["marker"]))}


def run_release(held):
    bucket, document, _ = opened(held, "release")
    context = document["context"]
    published = releasing.Release(context["repository"], context["marker"], context["commit"], held["wharf"])
    directories = {target["target"]: bound(bucket, document, target["name"]) for target in BUILD["targets"] if f"bind-{target['name']}" in document["entries"]}
    listed = declared(held["source"])
    installed = {target: executables.installed(listed, target) for target in directories}
    placed = placements(bucket, document, held["source"])
    managers = place()
    releasing.render(published, str(managers), installed)
    contents = releasing.Contents(directories, managers, installed, placed, lambda: guard.reported(published, directories))
    execution.ready(held)
    return releasing.publish(published, contents, r2.writer(releasing.place(published)[1], "RELEASES"))


def cfworker_deploy(held):
    bucket, _, entry = opened(held, "cfworker")
    held_basis = cfworker.basis(held["source"], RUNNER)
    resolved(entry, held_basis, (["lib.media.cfworker"], []))
    output = place()
    cfworker.deploy(cfworker.Deploy(Path(held["source"]), output, held.get("publication", held.get("confirm"))))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, held_basis, carried(held)))


def step(held):
    if held["unit"] not in LAYERED:
        raise Refusal(f"a layer runs one of {', '.join(LAYERED)}, not {held['unit']}")
    handler, names = ACTIONS[held["unit"]]
    values, origins = parameters.resolve(held["unit"], names, [])
    print("\n".join(parameters.report(values, origins)), file=sys.stderr)
    return execution.perform(held["unit"], handler, values)


ACTIONS = {
    "binary": (run_binary, ["source", "target", *PLANNED]),
    "suite": (run_suite, ["source", *PLANNED]),
    "bind": (run_bind, [*PLANNED]),
    "smoke": (run_smoke, ["target", *PLANNED]),
    "validate": (validate, ["source", *PLANNED]),
    "node-suite": (node_suite, ["source", *PLANNED]),
    "npm-pack": (execution.pack, ["source", *PLANNED]),
    "deb": (run_deb, ["source", *PLANNED]),
    "deb-verify": (run_verify, ["source", *PLANNED]),
    "release": (run_release, ["source", *PLANNED]),
    "cfworker-deploy": (cfworker_deploy, ["source", *PLANNED]),
    "step": (step, ["unit"]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("ship", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = execution.perform(action, handler, values)
    except Refusal as refusal:
        print(f"ship: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
