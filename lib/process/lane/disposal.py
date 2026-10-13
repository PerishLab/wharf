import os
import stat
from pathlib import Path

from lib.content import canonical
from lib.content.static import evidence
from lib.process.cfworker import discard, reader, writer
from lib.process.lane import build, entry, execution, provider, reservation
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents


def spent(value, environ, job="dispose"):
    context = entry.platform(environ)
    if job not in ("dispose", "apply") or environ.get("GITHUB_JOB") != job:
        raise Refusal("lane writer requires its fixed original job")
    root = Path(environ.get("RUNNER_TEMP", ""))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or any(parent.is_symlink() for parent in root.parents):
        raise Refusal("lane writer requires a real prepared runner temporary directory")
    document = reservation.custody(value["request"], context)
    body = documents.encode(document)
    path = root / f"wharf-lane-{job}-{context['run']}-{context['attempt']}-{document['request']}.json"
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        observed = path.lstat()
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or path.read_bytes() != body:
            raise Refusal("lane writer attempt marker failed exact local readback")
    except OSError:
        raise Refusal("lane writer attempt is spent or unavailable; no retry") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def perform(held, environ=os.environ):
    forbidden = tuple(name for name in build.FORBIDDEN if name != "WHARF_LANE_PROVIDER_TOKEN") + ("WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN")
    if any(r2.secret(environ, name) for name in forbidden):
        raise Refusal("lane disposal holds only its provider writer and prepared readers")
    intent = entry.requested(held["request"])
    if intent["operation"] != "dispose":
        raise Refusal("lane disposal accepts only exact dispose dispatch")
    before = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    value = before["prepared"]
    supplied = documents.object_value(evidence.decode(held["provider"]), ("execution", "plan", "operation"))
    if supplied["operation"] is not None or supplied["execution"] != canonical.digest(before):
        raise Refusal("lane disposal preflight belongs to another exact execution")
    policy = provider.policy(environ)
    client = reader.Reader(r2.secret(environ, "WHARF_LANE_PROVIDER_READER_TOKEN"), value["registration"])
    observed = client.status(policy)
    plan = discard.prepare(value["registration"], value["current"], observed, policy)
    if plan != supplied["plan"]:
        raise Refusal("lane disposal registration, reservation or complete provider preflight moved")
    if execution.observe(intent, value, environ) != before:
        raise Refusal("lane disposal authority moved before its single write")
    spent(value, environ)
    operation = None
    if observed["preview"] is not None:
        owned = writer.Writer(r2.secret(environ, "WHARF_LANE_PROVIDER_TOKEN"), plan)
        fresh = client.status(policy)
        if execution.observe(intent, value, environ) != before:
            raise Refusal("lane disposal authority moved before provider DELETE")
        result = owned.delete(fresh)
        if not result["acknowledged"]:
            operation = discard.unknown(plan, "unverified")
    if operation is None:
        operation = discard.settle(plan, client.status(policy), "idle")
    if execution.observe(intent, value, environ) != before:
        raise Refusal("lane disposal authority moved after execution; no result admitted")
    return {"execution": canonical.digest(before), "plan": plan, "operation": operation}
