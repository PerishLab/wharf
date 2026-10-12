import os
import tempfile
from pathlib import Path

from lib.cargo import toolchain
from lib.content import canonical
from lib.content.static import evidence, workspace
from lib.media import node
from lib.process.cfworker import apply
from lib.process.lane import entry
from lib.refusal import Refusal
from lib.store import handoff, r2
from lib.store.lane import registration

FORBIDDEN = ("CLOUDFLARE_API_TOKEN", "WHARF_LANE_STATE_ACCESS_KEY_ID", "WHARF_LANE_STATE_SECRET_ACCESS_KEY",
             "WHARF_LANE_EVIDENCE_ACCESS_KEY_ID", "WHARF_LANE_EVIDENCE_SECRET_ACCESS_KEY", "WHARF_RELEASES_ACCESS_KEY_ID",
             "WHARF_RELEASES_SECRET_ACCESS_KEY", "WHARF_LANE_PROVIDER_TOKEN")


def record(held, environ=os.environ):
    if any(r2.secret(environ, name) for name in FORBIDDEN):
        raise Refusal("lane build control may hold only the workload writer")
    intent = entry.requested(held["request"])
    before = evidence.decode(held["admitted"])
    selected = entry.source(intent, before)
    previewed = entry.platform(environ)
    control = before.get("workflow", {})
    if any(control.get(key) != previewed[key] for key in ("run", "attempt", "commit")) or control.get("path") != entry.policy()["workflow"]:
        raise Refusal("lane build admission belongs to another exact control run")
    acquired = {"repository": selected["repository"], **{key: selected["source"][key] for key in ("commit", "tree")}}
    workspace.acquired(Path(held["source"]), acquired)
    if toolchain.resolve() != toolchain.versions(environ):
        raise Refusal("lane build domain moved since preparation")
    bucket = r2.configured(environ)
    with tempfile.TemporaryDirectory(prefix="wharf-lane-handoff-") as directory:
        destination = Path(directory) / "content"
        request = node.Preview(Path(held["source"]), destination, selected, before["registration"])
        produced = node.preview_build(request, environ=environ)
        registration.checked(before["registration"])
        handoff.publish(bucket, destination)
        document, _ = handoff.fetch(bucket, produced["key"])
        if document != produced:
            raise Refusal("recorded lane workload differs from the exact built content")
        return {"intent": canonical.digest(intent), "workload": document["key"], "artifact": apply.identity(document)}
