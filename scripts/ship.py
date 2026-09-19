import argparse
import json
import sys
from pathlib import Path

from lib import canonical, implementation
from lib.cargo import basis, build, publish, suite, version
from lib.identity import bind
from lib.refusal import Refusal
from lib.identity.smoke import configured, smoke
from lib.media import cfworker, chart, node, npm, oci, release as releasing
from lib.store import r2
from lib.store import workload

RELEASE = ("repository", "marker", "commit", "tree")


def release(args):
    return bind.Release(args.repository, args.marker, args.commit, args.tree)


def keyed(held, modules, args):
    Path(args.basis).write_bytes(canonical.encode(held))
    return {"key": workload.key(held, implementation.resourced(*modules))}


def key_binary(args):
    held = basis.resolve(args.source, args.name, args.target, args.runner)
    return keyed(held, (["lib.cargo.basis", "lib.cargo.build"], []), args)


def key_suite(args):
    held = basis.suite(args.source, args.runner)
    return keyed(held, (["lib.cargo.basis", "lib.cargo.suite"], []), args)


def key_bind(args):
    identity = {field: getattr(args, field) for field in RELEASE}
    held = {"entry": {"kind": "binary-identity", "binary": args.binary_key}, "identity": identity}
    return keyed(held, (["lib.identity.bind"], ["identity/format.json"]), args)


def key_smoke(args):
    held = {"entry": {"kind": "binary-smoke", "binary": args.binary_key}}
    return keyed(held, (["lib.identity.smoke"], []), args)


def run_binary(args):
    return build.build(build.Build(Path(args.source), args.name, args.target, Path(args.output)))


def run_suite(args):
    return suite.suite(suite.Suite(Path(args.source), Path(args.output)))


def run_bind(args):
    return bind.perform(bind.Artifact(Path(args.dir), args.name, args.target), release(args), args.binary_key, args.output)


def validate(args):
    return configured(bind.Artifact(Path(args.dir), args.name, args.target), args.repository, args.marker)


def run_smoke(args):
    return smoke(bind.Artifact(Path(args.dir), args.name, args.target), args.output, args.expect)


def key_node(args):
    held = node.basis(args.source, args.runner)
    return keyed(held, (["lib.media.node"], []), args)


def node_engines(args):
    return node.declared(args.source)


def node_suite(args):
    return node.suite(node.Suite(Path(args.source), Path(args.output)))


def npm_plan(args):
    held = npm.pending(args.source, version.marker(args.marker))
    return {"published": all(item["published"] for item in held), "packages": held}


def npm_publish(args):
    return npm.publish(args.source, version.marker(args.marker))


def oci_plan(args):
    image = oci.reference(args.repository, version.marker(args.marker))
    return {"image": image, "published": oci.exists(image)}


def oci_publish(args):
    artifact = bind.Artifact(Path(args.dir), args.repository.split("/", 1)[1], args.target)
    image = oci.reference(args.repository, version.marker(args.marker))
    return oci.publish(oci.Image(Path(args.source), artifact.file, artifact.name, image))


def chart_plan(args):
    held = chart.pending(args.source, args.repository.split("/", 1)[0], version.marker(args.marker))
    return {"published": all(item["published"] for item in held), "charts": held}


def chart_publish(args):
    return chart.publish(args.source, args.repository.split("/", 1)[0], version.marker(args.marker))


def published_release(args):
    return releasing.Release(args.repository, args.marker, args.commit, args.wharf)


def release_plan(args):
    held = published_release(args)
    return {"channel": releasing.channel(held.marker), "published": releasing.published(held)}


def release_managers(args):
    held = published_release(args)
    binary = bind.Artifact(Path(args.dir), args.name, "x86_64-unknown-linux-gnu").file
    return releasing.render(held, binary, args.source, args.output)


def release_publish(args):
    held = published_release(args)
    bound = {"x86_64-unknown-linux-gnu": args.linux, "x86_64-pc-windows-msvc": args.windows, "aarch64-apple-darwin": args.macos}
    return releasing.publish(held, releasing.Contents(bound, Path(args.managers)), r2.writer(releasing.place(held)[1], "RELEASES"))


def key_cfworker(args):
    held = cfworker.basis(args.source, args.runner)
    return keyed(held, (["lib.media.cfworker"], []), args)


def cfworker_deploy(args):
    return cfworker.deploy(cfworker.Deploy(Path(args.source), Path(args.output)))


def cargo_plan(args):
    held = publish.pending(args.source, version.marker(args.marker))
    return {"published": all(item["published"] for item in held), "packages": held}


def cargo_publish(args):
    bound = version.inject(args.source, version.marker(args.marker))
    return publish.publish(args.source, bound["version"])


def command(actions, name, handler, options):
    parser = actions.add_parser(name)
    for option in options:
        parser.add_argument(f"--{option}", required=True)
    parser.set_defaults(handler=handler)


def parser():
    root = argparse.ArgumentParser(prog="ship")
    actions = root.add_subparsers(dest="action", required=True)
    command(actions, "key-binary", key_binary, ["source", "name", "target", "runner", "basis"])
    command(actions, "key-suite", key_suite, ["source", "runner", "basis"])
    command(actions, "key-bind", key_bind, ["binary-key", *RELEASE, "basis"])
    command(actions, "key-smoke", key_smoke, ["binary-key", "basis"])
    command(actions, "binary", run_binary, ["source", "name", "target", "output"])
    command(actions, "suite", run_suite, ["source", "output"])
    command(actions, "bind", run_bind, ["dir", "name", "target", *RELEASE, "binary-key", "output"])
    command(actions, "smoke", run_smoke, ["dir", "name", "target", "output", "expect"])
    command(actions, "validate", validate, ["dir", "name", "target", "repository", "marker"])
    command(actions, "key-node", key_node, ["source", "runner", "basis"])
    command(actions, "node-engines", node_engines, ["source"])
    command(actions, "node-suite", node_suite, ["source", "output"])
    command(actions, "npm-plan", npm_plan, ["source", "marker"])
    command(actions, "npm-publish", npm_publish, ["source", "marker"])
    command(actions, "oci-plan", oci_plan, ["repository", "marker"])
    command(actions, "oci-publish", oci_publish, ["source", "dir", "target", "repository", "marker"])
    command(actions, "chart-plan", chart_plan, ["source", "repository", "marker"])
    command(actions, "chart-publish", chart_publish, ["source", "repository", "marker"])
    command(actions, "release-plan", release_plan, ["repository", "marker", "commit", "wharf"])
    command(actions, "release-managers", release_managers, ["repository", "marker", "commit", "wharf", "dir", "name", "source", "output"])
    command(actions, "release-publish", release_publish, ["repository", "marker", "commit", "wharf", "linux", "windows", "macos", "managers"])
    command(actions, "key-cfworker", key_cfworker, ["source", "runner", "basis"])
    command(actions, "cfworker-deploy", cfworker_deploy, ["source", "output"])
    command(actions, "cargo-plan", cargo_plan, ["source", "marker"])
    command(actions, "cargo-publish", cargo_publish, ["source", "marker"])
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.handler(args)
    except Refusal as refusal:
        print(f"ship: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
