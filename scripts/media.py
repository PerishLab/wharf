import json
import shutil
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.cargo import publish, toolchain, version
from lib.content import executables, resources
from lib.identity import bind
from lib.identity.guard import execution
from lib.media import chart, node, npm, oci, release
from lib.refusal import Refusal
from lib.process import git, run, stream
from lib.store import plan, r2, workload

RELEASE = ("repository", "marker", "commit", "tree")
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")
PLANNED = (*CONTEXT, "planned")
BUILD = resources.read_json("build/config.json")
PRIMARY = next(target for target in BUILD["targets"] if target["name"] == BUILD["primary"])


def identified(held):
    return bind.Release(held["repository"], held["marker"], held["commit"], held["tree"])


def decided(published, held, carried=True):
    return dict(parameters.answer({"decision": "skip" if published else "run", "presence": "present" if carried else "none"}), **held)


def engines(held):
    versions = toolchain.resolve()
    answered = {"domain": toolchain.encoded(versions)}
    answered.update(node=versions["node.version"], pnpm=versions["pnpm.version"])
    return parameters.answer(answered)


def npm_plan(held):
    if not npm.carried(held["source"]):
        return decided(True, {"packages": []}, False)
    pending = npm.pending(held["source"], version.marker(held["marker"]))
    parameters.answer({"existing": "true" if any(item["published"] for item in pending) else "false"})
    return decided(all(item["published"] for item in pending), {"packages": pending}, bool(pending))


def npm_publish(held):
    bucket = r2.configured()
    document = plan.read(bucket, dict({field: held[field] for field in CONTEXT}, attempt=held["planned"]))
    plan.planned(document, "npm")
    directory = Path(tempfile.mkdtemp()) / "archives"
    workload.fetch(bucket, document["entries"]["pack-npm"]["key"], str(directory))
    return npm.publish(held["source"], version.marker(held["marker"]), directory, npm.Tools(run=execution.writer(held, run)))


def oci_plan(held):
    if not oci.carried(held["source"]):
        return decided(True, {"image": None}, False)
    items = oci.components(held["source"], held["repository"], version.marker(held["marker"]), verify=True)
    if not items[0].multi:
        item = items[0]
        if item.kind == "source":
            return decided(False, {"image": item.reference})
        return decided(oci.exists(item.reference) and oci.current(item.reference), {"image": item.reference})
    checked = [
        {"name": item.name, "image": item.reference, "kind": item.kind, "ready": False if item.kind == "source" else (oci.exists(item.reference) and oci.current(item.reference))}
        for item in items
    ]
    return decided(all(entry["ready"] for entry in checked), {"components": checked})


def oci_entry(item, published, channel):
    digest = published.get("digest")
    if digest is None:
        digests = published.get("digests") or []
        digest = digests[0].rsplit("@", 1)[1] if digests and "@" in digests[0] else None
    status = published.get("status") or published.get("state")
    return {"name": item.name, "image": item.reference, "digest": digest, "status": status, "channel": channel}


def oci_publish_one(held, item, bound, confirm):
    if item.kind == "binary":
        artifact = bind.Artifact(bound, item.binary, PRIMARY["target"])
        request = oci.Image(Path(held["source"]), artifact.file, item.binary, item.reference, held["marker"], item.file)
        published = oci.publish(request, run, confirm)
    elif item.kind == "web":
        request = oci.WebImage(Path(held["source"]), item.web, item.package, item.file, item.reference)
        published = oci.publish_web(request, run, confirm)
    else:
        request = oci.SourceImage(Path(held["source"]), item.reference, item.file)
        published = oci.publish_source(request, run, confirm)
    return published, oci.advance(item.reference, run, confirm)


def oci_publish(held):
    bucket = r2.configured()
    document = plan.read(bucket, dict({field: held[field] for field in CONTEXT}, attempt=held["planned"]))
    plan.planned(document, "oci")
    items = oci.components(held["source"], held["repository"], version.marker(held["marker"]), verify=False)
    if not items:
        raise Refusal("the product declares no image")
    listed = executables.declared(held["source"], release.targets(held["source"]))
    built = executables.built(listed, PRIMARY["target"])
    for item in items:
        if item.kind == "binary" and item.binary not in built:
            raise Refusal(f"[release.oci] carries {item.binary}, which is not built for {PRIMARY['target']}")
    directory = None
    if any(item.kind == "binary" for item in items):
        directory = Path(tempfile.mkdtemp()) / "bound"
        workload.fetch(bucket, document["entries"][f"bind-{PRIMARY['name']}"]["key"], str(directory))
    confirm = lambda: execution.ready(held)
    try:
        if not items[0].multi:
            published, channel = oci_publish_one(held, items[0], directory, confirm)
            return dict(published, channel=channel)
        results = []
        for item in items:
            published, channel = oci_publish_one(held, item, directory, confirm)
            results.append(oci_entry(item, published, channel))
        return {"components": results}
    finally:
        if directory is not None:
            shutil.rmtree(directory.parent, ignore_errors=True)


def release_plan(held):
    published = release.Release(held["repository"], held["marker"], held["commit"], held["wharf"])
    done, carried = release.presence(held["source"], published)
    return decided(done, {"channel": release.channel(held["marker"])}, carried)


def chart_plan(held):
    pending = chart.pending(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]))
    parameters.answer({"existing": "true" if any(item["published"] for item in pending) else "false"})
    return decided(all(item["published"] for item in pending), {"charts": pending}, bool(pending))


def chart_publish(held):
    return chart.publish(held["source"], held["repository"].split("/", 1)[0], version.marker(held["marker"]), chart.Tools(run=run, confirm=lambda: execution.ready(held)))


def cargo_plan(held):
    pending = publish.proven(held["source"], identified(held))
    parameters.answer({"existing": "true" if any(item["published"] for item in pending) else "false"})
    return decided(all(item["published"] for item in pending), {"packages": pending}, bool(pending))


def cargo_publish(held):
    with tempfile.TemporaryDirectory(prefix="wharf-cargo-binding-") as directory:
        source = Path(directory) / "product"
        git(held["source"], "clone", "--no-hardlinks", "--local", str(held["source"]), str(source))
        bound = version.inject(source, identified(held))
        return publish.publish(source, bound["version"], publish.Tools(run=stream, confirm=lambda: execution.ready(held)))


ACTIONS = {
    "engines": (engines, ["source"]),
    "npm-plan": (npm_plan, ["source", "marker"]),
    "npm-publish": (npm_publish, ["source", *PLANNED]),
    "oci-plan": (oci_plan, ["source", "repository", "marker"]),
    "oci-publish": (oci_publish, ["source", *PLANNED]),
    "release-plan": (release_plan, ["repository", "marker", "commit", "wharf", "source"]),
    "chart-plan": (chart_plan, ["source", "repository", "marker"]),
    "chart-publish": (chart_publish, ["source", *PLANNED]),
    "cargo-plan": (cargo_plan, ["source", *RELEASE]),
    "cargo-publish": (cargo_publish, ["source", "commit", "tree", *PLANNED]),
}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("media", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = execution.perform(action, handler, values)
    except Refusal as refusal:
        print(f"media: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
