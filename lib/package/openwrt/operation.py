import hashlib
from pathlib import Path

from lib.content import declaration, executables, implementation
from lib.identity import bind
from lib.identity.guard import execution
from lib.package.openwrt import package, signing, verify
from lib.package.openwrt.version import version
from lib.refusal import Refusal
from lib.store import plan, r2, workload

PACK = (["lib.package.openwrt.package"], ["build/config.json", "releases.json", "build/apk.sh"])
VERIFY = (["lib.package.openwrt.verify"], ["build/config.json"])
CONTEXT = ("repository", "marker", "wharf", "run", "attempt")


def declared(source, product):
    listed = executables.declared(source, declaration.release(source).get("targets", []))
    binary = executables.placed(source, "apk", listed, product)
    if binary is None:
        return None
    if binary not in executables.built(listed, package.PROFILE["target"]):
        raise Refusal("OpenWrt APK executable must carry the ARM64 musl target")
    return package.declared(source, binary)


def opened(held, name):
    context = dict({field: held[field] for field in CONTEXT}, attempt=held["planned"])
    bucket = r2.configured()
    document = plan.read(bucket, context)
    return bucket, document, plan.planned(document, name)


def packed(held):
    bucket, document, entry = opened(held, "apk")
    context = document["context"]
    selected = declared(held["source"], plan.product(context))
    binary = document["entries"]["bind-arm64"]["key"]
    basis = package.basis(held["source"], selected, binary, version(context["marker"]))
    if workload.key(basis, implementation.resourced(*PACK)) != entry["key"]:
        raise Refusal("OpenWrt APK build differs from this run's frozen plan")
    bound = bind.Artifact(workload.staged(bucket, binary), selected.binary, package.PROFILE["target"]).file
    output = workload.place()
    package.build(package.Package(Path(held["source"]), selected, bound, version(context["marker"]), output))
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, basis, {field: held[field] for field in CONTEXT}))


def verified(held):
    bucket, document, entry = opened(held, "verify-apk")
    context = document["context"]
    selected = declared(held["source"], plan.product(context))
    apk = document["entries"]["apk"]["key"]
    binary = document["entries"]["bind-arm64"]["key"]
    basis = {"entry": {"kind": "apk-verify", "apk": apk, "binary": binary}}
    if workload.key(basis, implementation.resourced(*VERIFY)) != entry["key"]:
        raise Refusal("OpenWrt APK verification differs from this run's frozen plan")
    bound = bind.Artifact(workload.staged(bucket, binary), selected.binary, package.PROFILE["target"]).file
    file = workload.staged(bucket, apk) / package.named(selected.binary, version(context["marker"]))
    check = verify.Check(file, selected.binary, context["marker"], hashlib.sha256(bound.read_bytes()).hexdigest(), version(context["marker"]), tuple(package.metadata(held["source"], selected).get("depends", [])))
    output = workload.place()
    verify.verify(check, output)
    return execution.publish(held, bucket, entry["key"], workload.Produced(output, basis, {field: held[field] for field in CONTEXT}))


def placed(bucket, document, source):
    context, entries = document["context"], document["entries"]
    selected = declared(source, plan.product(context))
    if selected is None:
        return {}
    if "apk" not in entries or "verify-apk" not in entries or not workload.reusable(bucket, entries["verify-apk"]["key"]):
        raise Refusal("OpenWrt APK release requires its recorded native verification")
    file = workload.staged(bucket, entries["apk"]["key"]) / package.named(selected.binary, version(context["marker"]))
    authority = f"https://releases.{plan.product(context)}.perish.uk"
    return signing.placed(file, authority, workload.place())
