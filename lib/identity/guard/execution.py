import json
import tempfile
from dataclasses import replace
from pathlib import Path

from lib import parameters
from lib.cargo import toolchain
from lib.cargo.version import marker
from lib.content import implementation
from lib.identity.guard import snapshot
from lib.media import npm
from lib.refusal import Refusal
from lib.store import plan, r2, workload

BUILDING = {"binary", "suite", "node-suite", "cfworker-deploy", "npm-pack"}
PUBLISHING = {"npm-publish", "oci-publish", "cargo-publish", "chart-publish", "release", "cfworker-deploy"}
CONTEXT = ("repository", "marker", "wharf", "run")
IDENTITY = ("repository", "marker", "commit", "tree")


def perform(action, handler, held):
    if action not in BUILDING | PUBLISHING:
        return handler(held)
    context = dict({field: held[field] for field in CONTEXT}, attempt=held["planned"])
    bucket = r2.configured()
    document = plan.read(bucket, context)
    expected = document["context"].get("snapshot")
    if expected is None:
        raise Refusal("this build needs a verified release snapshot in its plan")
    identity = {field: document["context"][field] for field in IDENTITY}
    request = snapshot.Request(Path(held["source"]), identity, toolchain.versions(), expected, snapshot.destination(held["source"]))
    with snapshot.prepared(request) as (root, record, confirm):
        scoped = dict(held, source=root, snapshot=record, confirm=confirm)
        if action in PUBLISHING:
            scoped["publication"] = lambda: admit(bucket, document, request, confirm)
        return handler(scoped)


def admit(bucket, document, request, confirm):
    confirm()
    with snapshot.prepared(replace(request, destination=None)) as (_, _, current):
        current()
        confirm()
        plan.reserve(bucket, document)
        parameters.answer({"snapshot": json.dumps(plan.combination(document["context"]["snapshot"]), separators=(",", ":"), sort_keys=True)})


def ready(held):
    if "publication" in held:
        held["publication"]()


def writer(held, runner):
    def invoke(argv, cwd, env=None):
        ready(held)
        return runner(argv, cwd, env)
    return invoke


def publish(held, bucket, key, produced):
    if "confirm" in held:
        held["confirm"]()
    return workload.publish(bucket, key, produced)


def pack(held):
    bucket = r2.configured()
    context = dict({field: held[field] for field in CONTEXT}, attempt=held["planned"])
    document = plan.read(bucket, context)
    entry = plan.planned(document, "pack-npm")
    source = held["source"]
    basis = npm.basis(source, held["marker"])
    key = workload.key(basis, implementation.resourced(["lib.media.npm"], []))
    if key != entry["key"]:
        raise Refusal("npm archive content differs from the planned workload")
    with tempfile.TemporaryDirectory(prefix="wharf-npm-") as directory:
        output = Path(directory) / "archives"
        npm.prepare(source, marker(held["marker"]), output)
        return publish(held, bucket, key, workload.Produced(output, basis, context))
