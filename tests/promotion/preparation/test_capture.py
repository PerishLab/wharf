import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.promotion import capture, preflight
from lib.refusal import Refusal
from lib.store import plan

OBSERVER = {"run": "9", "attempt": "1", "wharf": "e" * 40}
SOURCE = {"repository": "PerishLab/plumb", "marker": "v1.2.3", "commit": "a" * 40, "tree": "b" * 40}
REQUEST = {"repository": SOURCE["repository"], "marker": SOURCE["marker"], "semantics": '{"kind":"capture","schema":"wharf.release.semantics.request/v1"}'}


def world():
    return {"schema": "wharf.release.snapshot/v1", "source": SOURCE, "head": "f" * 40, "tree": "b" * 40, "packages": [],
            "controls": {"plumb": "plumb v0.138.0", "ectropy": "ectropy v0.51.0", "authority": {"producer": "v0.138.0@" + "c" * 40, "depot": "d" * 64}, "stable": {"plumb": {"releaseVersion": "v0.138.0", "commit": "c" * 40}, "ectropy": {"releaseVersion": "v0.51.0", "commit": "e" * 40}}},
            "domain": {"node.version": "24.18.0", "pnpm.version": "11.28.5", "rust.version": "1.96.1"},
            "guard": {"schema": "plumb.guard-runtime/v1", "root": "/fixture/source", "ok": True, "strength": "full", "boundary": "head", "commit": "f" * 40,
                      "guard": {"schema": "plumb.guard-proof/v1", "repository": SOURCE["repository"], "tree": "b" * 40, "plumb": "v0.138.0@" + "c" * 40, "depot": "d" * 64, "platform": "linux-x86_64", "actions": [{"name": "guard/test", "input": "a" * 64, "world": "e" * 64}], "resolution": {"context": "ci-latest", "tree": "b" * 40, "packages": []}, "digest": "b8727631dd07dd79e7331ddc27cbdf2edcff4e02fd4fa33611bd76d2841e0bf1"},
                      "digest": "9be542a1a4f976d8813a212dac3216d67e907f44346c98df85833e1d9d481c91"}}


class Capture(unittest.TestCase):
    def test_selection_uses_actual_native_role_and_refuses_peer_execution(self):
        record = {"schema": "wharf.release.semantics.family.selection/v1", "product": "plumb", "marker": SOURCE["marker"], "root": {}, "family": {}, "history": {}, "previous": None, "sequence": 1}
        request = {"schema": "wharf.release.semantics.request/v1", "kind": "selection", "record": record}
        selected = REQUEST | {"semantics": canonical.encode(request).decode()}
        held = {"observer": OBSERVER, "repository": SOURCE["repository"], "marker": SOURCE["marker"]}
        jobs = [{"id": 19, "name": preflight.ROLES["semantics-select"]}]
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, jobs)), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(capture.control, "api", side_effect=self.api):
            self.assertEqual(capture.active(held, "semantics-select")["job"], 19)
            jobs.append({"id": 20, "name": "[publish] cargo", "started_at": "now", "conclusion": None})
            with self.assertRaises(Refusal):
                capture.active(held, "semantics-select")

    def test_transition_uses_the_real_issuer_role_and_excludes_peer_execution(self):
        intent = {name: None for name in ("product", "marker", "root", "previous", "anchor", "request", "scenes", "world", "review", "direct")}
        request = {"schema": "wharf.release.semantics.request/v1", "kind": "transition", "mode": "historical", "record": {"schema": "wharf.release.semantics.transition/v1", "intent": intent, "history": {}, "source": None}}
        selected = REQUEST | {"semantics": canonical.encode(request).decode()}
        held = {"observer": OBSERVER, "repository": SOURCE["repository"], "marker": SOURCE["marker"]}
        jobs = [{"id": 19, "name": preflight.ROLES["semantics-issue"]}]
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, jobs)), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(capture.control, "api", side_effect=self.api):
            self.assertEqual(capture.active(held, "semantics-issue")["job"], 19)
            jobs.append({"id": 20, "name": "[publish] cargo", "started_at": "now", "conclusion": None})
            with self.assertRaises(Refusal):
                capture.active(held, "semantics-issue")

    def test_history_uses_actual_role_kind_lineage_and_excludes_other_execution(self):
        held = {"observer": OBSERVER, "repository": SOURCE["repository"], "marker": SOURCE["marker"]}
        value = {"kind": "history", "schema": "wharf.release.semantics.request/v1", "root": {"fixture": "shared validates"}, "previous": None}
        selected = REQUEST | {"semantics": canonical.encode(value).decode()}
        job = {"id": 19, "name": preflight.ROLES["semantics-history"]}
        jobs = [job]
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, jobs)), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(capture.control, "api", side_effect=self.api):
            self.assertEqual(capture.active(held, "semantics-history"), {"run": 9, "attempt": 1, "commit": OBSERVER["wharf"], "job": 19})
            jobs.append({"id": 20, "name": "[publish] cargo", "started_at": "now", "conclusion": None})
            with self.assertRaises(Refusal):
                capture.active(held, "semantics-history")
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [job])), mock.patch.object(preflight, "event", return_value=preflight.selection(REQUEST)), self.assertRaises(Refusal):
            capture.active(held, "semantics-history")

    def test_capture_and_issue_roles_require_their_actual_request_kind(self):
        job = {"id": 19, "name": preflight.ROLES["semantics-issue"]}
        held = {"observer": OBSERVER, "repository": SOURCE["repository"], "marker": SOURCE["marker"]}
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [job])), mock.patch.object(preflight, "event", return_value=preflight.selection(REQUEST)), self.assertRaises(Refusal):
            capture.active(held, "semantics-issue")
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [job])), mock.patch.object(preflight, "event", return_value=preflight.selection(REQUEST)), self.assertRaises(Refusal):
            capture.active(held, "semantics-world")
        value = {"kind": "world", "schema": "wharf.release.semantics.request/v1", "commit": SOURCE["commit"], "scenes": {"fixture": "selected"}}
        selected = REQUEST | {"semantics": canonical.encode(value).decode()}
        job = {"id": 19, "name": preflight.ROLES["semantics-world"]}
        with mock.patch.object(preflight, "active", return_value=(OBSERVER, [job])), mock.patch.object(preflight, "event", return_value=preflight.selection(selected)), mock.patch.object(capture.control, "api", side_effect=self.api):
            self.assertEqual(capture.active(held, "semantics-world")["job"], 19)

    def fixture(self):
        context = SOURCE | {"wharf": "c" * 40, "run": "7", "attempt": "1", "snapshot": world()}
        document = {"schema": 1, "context": context, "entries": {}, "engines": {}, "shape": {}}
        reference = plan.reference(document)
        distribution = {"schema": 1, **SOURCE, "state": "complete", "plan": reference, "resolution": {"binaries": plan.combination(context["snapshot"])}}
        return document, distribution

    def api(self, path):
        repository = {"id": 1374358737, "full_name": "PerishLab/wharf"}
        if path == "branches/main":
            return {"name": "main", "protected": True, "commit": {"sha": OBSERVER["wharf"]}}
        if path.startswith("compare/"):
            source = path.removeprefix("compare/").split("...")[0]
            return {"base_commit": {"sha": source}, "merge_base_commit": {"sha": source}, "status": "identical" if source == OBSERVER["wharf"] else "ahead"}
        if path.endswith("/jobs?per_page=100"):
            return {"total_count": 1, "jobs": [{"id": 11, "name": preflight.ROLES["plan"], "status": "completed", "conclusion": "success", "run_id": 7, "run_attempt": 1, "head_sha": "c" * 40, "head_branch": "main"}]}
        return {"id": 7, "run_attempt": 1, "head_sha": "c" * 40, "repository": repository, "head_repository": repository, "head_branch": "main", "path": ".github/workflows/ship.yml", "event": "workflow_dispatch", "status": "completed", "conclusion": "success"}

    def inspect(self, arguments):
        self.assertEqual(arguments[:3], ["release", "inspect", "--input"])
        original = json.loads(Path(arguments[3]).read_bytes())
        return 0, {"state": "observed", "publication": False, "facts": {"schema": "plumb.release.inspection/v1", "data": original, "authentication": False, "publication": False}}

    def test_actual_original_world_is_copied_without_new_resolution(self):
        document, distribution = self.fixture()
        bucket = mock.Mock()
        bucket.get.return_value = canonical.encode(document)
        job = {"id": 19, "name": preflight.ROLES["semantics"]}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "captured"
            held = {"observer": OBSERVER, "repository": SOURCE["repository"], "marker": SOURCE["marker"], "source": str(destination)}
            with mock.patch.object(preflight, "active", return_value=(OBSERVER, [job])), mock.patch.object(preflight, "event", return_value=preflight.selection(REQUEST)), mock.patch.object(capture.control, "api", side_effect=self.api), mock.patch.object(capture.release, "fetch", return_value=canonical.encode(distribution)), mock.patch.object(capture.r2, "configured", return_value=bucket), mock.patch.object(capture.reader, "identity", return_value={"producer": "v0.139.0@" + "d" * 40, "depot": "d" * 64}), mock.patch.object(capture.reader, "capture", side_effect=self.inspect):
                record = capture.produce(held)
            self.assertEqual((destination / "world.json").read_bytes(), canonical.encode(document["context"]["snapshot"]))
            self.assertEqual(record["native"], {"run": 9, "attempt": 1, "commit": "e" * 40, "job": 19})
            self.assertEqual(record["original"]["plan"], distribution["plan"])
            self.assertFalse(record["publication"] or record["adoption"] or record["activation"])
            bucket.get.assert_called_once_with("plan/PerishLab/plumb/v1.2.3/7-1.json")
            bucket.create.assert_not_called()
            bucket.put.assert_not_called()

    def test_missing_mismatched_or_different_effective_world_refuses(self):
        document, distribution = self.fixture()
        for change in ("missing", "source", "resolution"):
            changed = copy.deepcopy(document)
            if change == "missing":
                del changed["context"]["snapshot"]
            elif change == "source":
                changed["context"]["snapshot"]["source"]["commit"] = "0" * 40
            else:
                changed["context"]["snapshot"]["domain"]["rust.version"] = "1.0.0"
            with self.subTest(change=change), self.assertRaises(Refusal):
                capture.snapshot(distribution, changed)

    def test_native_lineage_and_incomplete_original_jobs_refuse(self):
        document, distribution = self.fixture()
        with mock.patch.object(capture.control, "api", side_effect=[{"name": "main", "protected": True, "commit": {"sha": "e" * 40}}, {"base_commit": {"sha": "c" * 40}, "merge_base_commit": {"sha": "0" * 40}, "status": "diverged"}]), self.assertRaises(Refusal):
            capture.lineage("c" * 40)
        api = self.api
        def incomplete(path):
            return {"total_count": 2, "jobs": []} if path.endswith("/jobs?per_page=100") else api(path)
        with mock.patch.object(capture.control, "api", side_effect=incomplete), self.assertRaises(Refusal):
            capture.original(distribution["plan"])

    def test_data_inspection_cannot_grant_authority_or_hide_unknown(self):
        original = world()
        for code, report in [(3, {"state": "unknown", "publication": False}), (0, {"state": "observed", "publication": True}), (0, {"state": "observed", "publication": False, "facts": {"schema": "plumb.release.inspection/v1", "data": original, "authentication": True, "publication": False}})]:
            with self.subTest(code=code), mock.patch.object(capture.reader, "identity", return_value={}), mock.patch.object(capture.reader, "capture", return_value=(code, report)), self.assertRaises(Refusal):
                capture.inspect(original, canonical.encode(original))
