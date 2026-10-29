import hashlib
import json
import tempfile
from pathlib import Path
import unittest

from unittest import mock

from lib import parameters
from scripts import distribution
from lib.process.trial import admission, consumer, judgment, declaration
from lib.refusal import Refusal

HELD = {"repository": "PerishLab/plumb", "marker": "v0.42.0", "commit": "a" * 40, "tree": "b" * 40, "wharf": "c" * 40, "run": "7", "attempt": "1", "needs": json.dumps({"plan": {"result": "success"}})}


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in distribution.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())


class Record(unittest.TestCase):
    def test_the_record_is_written_to_the_product_release_bucket_and_read_back_from_its_authority(self):
        seen = {}

        def record(bucket, context, needs, reader):
            seen.update(bucket=bucket, context=context, needs=needs)
            with mock.patch.object(distribution.release, "fetch", side_effect=lambda url: url):
                seen["read"] = reader("v1/releases/stable/v0.42.0/distribution.json")
            return {"state": "complete"}

        with mock.patch.object(distribution.r2, "writer", side_effect=lambda name, role: (name, role)), mock.patch.object(distribution.distribution, "record", side_effect=record), mock.patch.object(distribution.parameters, "answer") as answer:
            self.assertEqual(distribution.record(HELD), {"state": "complete"})
        answer.assert_called_once_with({"state": "complete"})
        self.assertEqual(seen["bucket"], ("perish-plumb-releases", "RELEASES"))
        self.assertEqual(seen["read"], "https://releases.plumb.perish.uk/v1/releases/stable/v0.42.0/distribution.json")
        self.assertEqual((seen["context"]["commit"], seen["needs"]), ("a" * 40, {"plan": {"result": "success"}}))


def publisher():
    return {"id": 7, "run_attempt": 1, "repository": {"full_name": "PerishLab/wharf"}, "head_repository": {"full_name": "PerishLab/wharf"}, "event": "workflow_dispatch", "path": ".github/workflows/ship.yml", "head_branch": "main", "head_sha": "a" * 40, "status": "completed", "display_title": "[ship] PerishLab/plumb v1.2.3-rc.1"}


def report():
    return {"schema": "seatrial.run/v1", "platform": "linux-x86_64", "runner": "a" * 64, "products": [{"name": "plumb"}], "ok": True, "state": "passed", "cleanup": True, "selected": ["writer"], "cases": [{"name": "writer", "passed": True, "error": None, "seconds": 1}], "seconds": 9, "stages": [{"name": name, "start": index, "seconds": 0.5, "ok": True} for index, name in enumerate(judgment.STAGES)], "scene": {"product": "seatrial", "kind": "scene", "channel": "stable", "manifest": {"url": "https://depot.seatrial.perish.uk/channels/stable/manifest.json"}}, "images": [{"name": "plumb", "digest": "ghcr.io/perishlab/images@sha256:" + "b" * 64}]}


class Admission(unittest.TestCase):
    def test_only_exact_trusted_nonstable_publisher_is_eligible(self):
        run = publisher()
        self.assertEqual(admission.candidate(run, "7"), {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"})
        for key, value in (("id", 8), ("path", ".github/workflows/other.yml"), ("head_branch", "topic"), ("status", "in_progress"), ("event", "pull_request"), ("head_repository", {"full_name": "Else/wharf"})):
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.candidate(dict(run, **{key: value}), "7")
        self.assertIsNone(admission.candidate(dict(run, display_title="[ship] PerishLab/plumb v1.2.3"), "7"))
        self.assertEqual(admission.candidate(dict(run, display_title="[ship] PerishLab/keel v1.2.3-rc.1"), "7")["repository"], "PerishLab/keel")

    def test_completion_belongs_to_exact_actual_attempt(self):
        run = publisher()
        held = {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1", "state": "complete", "commit": "b" * 40, "tree": "c" * 40, "completed": {"run": "7", "attempt": "1", "wharf": "a" * 40}, "media": {"binaries": "published"}}
        selected = {"repository": held["repository"], "marker": held["marker"]}
        self.assertFalse(admission.publication(run, selected, held)["eligible"])
        for changed in ({"state": "incomplete"}, {"completed": dict(held["completed"], attempt="2")}, {"completed": None}, {"commit": ""}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                admission.publication(run, selected, dict(held, **changed))


class Judgment(unittest.TestCase):
    def test_complete_success(self):
        judgment.complete(report(), {"exit": 0, "cases": ["writer"], "scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}}, {"sha256": "a" * 64})

    def test_failure_cancel_unknown_cleanup_and_incomplete_timing_refuse(self):
        mutations = [{"ok": False}, {"state": "cancelled"}, {"cleanup": None}, {"cleanup": False}, {"runner": "b" * 64}, {"cases": []}, {"selected": []}, {"stages": []}, {"seconds": float("nan")}, {"scene": None}, {"images": []}]
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                judgment.complete(dict(report(), **changed), {"exit": 0, "cases": ["writer"], "scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}}, {"sha256": "a" * 64})
        with self.assertRaises(Refusal):
            judgment.complete(report(), {"exit": 1, "cases": ["writer"], "scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}}, {"sha256": "a" * 64})

    def test_actual_product_archive_and_seal_must_match_publisher(self):
        seal = json.dumps({"product": "plumb", "channel": "rc", "releaseVersion": "v1.2.3-rc.1", "commit": "a" * 40, "artifacts": {"linux-x64": {"sha256": "b" * 64}}}).encode()
        expected = {"product": "plumb", "channel": "rc", "marker": "v1.2.3-rc.1", "commit": "a" * 40}
        product = {"name": "plumb", "channel": "rc", "version": expected["marker"], "commit": expected["commit"], "seal": hashlib.sha256(seal).hexdigest(), "archive": "b" * 64}
        judgment.identity({"products": [product]}, expected, seal)
        for field in ("version", "commit", "seal", "archive", "channel"):
            with self.subTest(field=field), self.assertRaises(Refusal):
                judgment.identity({"products": [dict(product, **{field: "different"})]}, expected, seal)

    def test_absent_report_preserves_unknown_and_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            (evidence / "admission.json").write_text('{"eligible":true}')
            (evidence / "execution.json").write_text('{"state":"executed"}')
            with self.assertRaises(FileNotFoundError):
                judgment.judge({"source": directory})
            self.assertEqual(json.loads((evidence / "observation.json").read_text())["state"], "unknown")

    def test_superseded_release_never_claims_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            evidence.mkdir()
            (evidence / "admission.json").write_text('{"eligible":true,"channel":"rc","marker":"v1.2.3-rc.1","authority":"https://releases.plumb.perish.uk"}')
            with mock.patch.object(consumer, "document", return_value={"releaseVersion": "v1.2.3-rc.2"}), mock.patch.object(consumer, "install") as install:
                result = consumer.execute({"source": directory, "trialcase": "writer"})
            install.assert_not_called()
            self.assertEqual(result["state"], "superseded")
            self.assertFalse(judgment.judge({"source": directory})["ok"])


class Declaration(unittest.TestCase):
    def specification(self):
        return {"scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "beta"}, "cases": ["health-lite", "again-lite"], "image": "ghcr.io/perishlab/keel"}

    def test_strict_declared_shapes(self):
        value = self.specification()
        self.assertEqual(declaration.validate(value), value)
        for change in ({"cases": []}, {"cases": ["x", "x"]}, {"cases": ["*"]}, {"command": ["sh"]}, {"image": "ghcr.io/perishlab/keel:0.27.0"}, {"scenes": dict(value["scenes"], channel="v0.1.2-beta.1")}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                declaration.validate(dict(value, **change))

    def test_wrong_source_and_manifest_refuse(self):
        source = {"repository": "PerishLab/keel", "marker": "v1.2.3-rc.1", "commit": "a" * 40, "tree": "b" * 40}
        value = {"schema": declaration.SCHEMA, "source": source, "manifest": "c" * 64, "authority": "https://releases.keel.perish.uk", "specification": self.specification()}
        self.assertEqual(declaration.bound(value, source), value)
        with self.assertRaises(Refusal):
            declaration.bound(value, dict(source, commit="d" * 40))
        with self.assertRaises(Refusal):
            declaration.bound(dict(value, manifest=""), source)

    def test_multicase_completion_cannot_hide_a_missing_or_duplicate_case(self):
        held = report()
        held["selected"] = ["health-lite", "again-lite"]
        held["cases"] = [{"name": name, "passed": True, "error": None, "seconds": 1} for name in held["selected"]]
        execution = {"exit": 0, "cases": held["selected"], "scenes": {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}}
        judgment.complete(held, execution, {"sha256": "a" * 64})
        with self.assertRaises(Refusal):
            judgment.complete(dict(held, cases=held["cases"][:1]), execution, {"sha256": "a" * 64})
        with self.assertRaises(Refusal):
            judgment.complete(dict(held, cases=[held["cases"][0], held["cases"][0]]), execution, {"sha256": "a" * 64})

    def test_publication_admits_each_declared_product_and_refuses_source_swap(self):
        for product in ("keel", "plumb"):
            run = dict(publisher(), display_title=f"[ship] PerishLab/{product} v1.2.3-rc.1")
            selected = admission.candidate(run, "7")
            source = dict(selected, commit="b" * 40, tree="c" * 40)
            trial = {"schema": declaration.SCHEMA, "source": source, "manifest": "d" * 64, "authority": f"https://releases.{product}.perish.uk", "specification": self.specification()}
            record = dict(source, state="complete", completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, media={"binaries": "published"}, trial=trial)
            self.assertTrue(admission.publication(run, selected, record)["eligible"])
            with self.assertRaises(Refusal):
                admission.publication(run, selected, dict(record, commit="e" * 40))

    def test_capture_uses_actual_manifest_and_no_suite_stays_undeclared(self):
        source = {"repository": "PerishLab/keel", "marker": "v1.2.3-rc.1", "commit": "a" * 40, "tree": "b" * 40}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plumb.toml"
            base = '[identity]\nns="keel"\n[release]\nauthority="https://releases.keel.perish.uk"\n'
            path.write_text(base)
            reply = {"schema": "plumb.windtun.declaration/v1", "manifest": hashlib.sha256(path.read_bytes()).hexdigest(), "windtun": None}
            with mock.patch.object(declaration.subprocess, "check_output", return_value=json.dumps(reply)):
                self.assertIsNone(declaration.capture(directory, source))
            for section in ("windtun", "guard.seatrial"):
                path.write_text(base + f'[{section}]\ncases=["health-lite","again-lite"]\nimage="ghcr.io/perishlab/keel"\n[{section}.scenes]\nauthority="https://depot.seatrial.perish.uk"\nproduct="seatrial"\nchannel="beta"\n')
                reply = dict(reply, manifest=hashlib.sha256(path.read_bytes()).hexdigest(), windtun=self.specification())
                with mock.patch.object(declaration.subprocess, "check_output", return_value=json.dumps(reply)) as inspected:
                    held = declaration.capture(directory, source)
                    self.assertEqual(inspected.call_args.args[0], ["plumb", "windtun", "inspect", directory, "--json"])
                    self.assertEqual(held["manifest"], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertEqual(held["source"], source)
                    with self.assertRaises(Refusal):
                        declaration.capture(directory, dict(source, repository="PerishLab/plumb"))

    def test_declared_cases_and_clean_environment_reach_generic_runner(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence").mkdir()
            admitted = {"eligible": True, "marker": "v1.2.3-rc.1", "channel": "rc", "product": "keel", "authority": "https://releases.keel.perish.uk", "trial": {"specification": self.specification()}}
            (root / "evidence/admission.json").write_text(json.dumps(admitted))
            with mock.patch.object(consumer, "document", return_value={"releaseVersion": admitted["marker"]}), mock.patch.object(consumer, "install", return_value=root / "seatrial"), mock.patch.object(consumer.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as invoked:
                result = consumer.execute({"source": str(root), "trialcase": "declared"})
            self.assertEqual(invoked.call_args.args[0][-4:], ["--case", "health-lite", "--case", "again-lite"])
            self.assertEqual(set(invoked.call_args.kwargs["env"]), {"PATH", "HOME", "LANG"})
            self.assertEqual(result["scope"], "declared")
            self.assertEqual(result["cases"], admitted["trial"]["specification"]["cases"])

    def test_one_marker_never_changes_its_published_suite(self):
        from lib.store import distribution as records
        source = {"repository": "PerishLab/keel", "marker": "v1.2.3-rc.1", "commit": "a" * 40, "tree": "b" * 40}
        held = dict(source, media={}, state="complete", completed={}, trial={"specification": self.specification()})
        changed = dict(held, trial={"specification": dict(self.specification(), cases=["health-lite"])})
        with self.assertRaises(Refusal):
            records.merged(held, changed)

    def test_undeclared_library_release_does_not_require_an_executable(self):
        run = publisher()
        selected = {"repository": "PerishLab/library", "marker": "v1.2.3-rc.1"}
        held = dict(selected, state="complete", commit="b" * 40, tree="c" * 40, completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, media={"binaries": "none"})
        self.assertFalse(admission.publication(run, selected, held)["eligible"])

    def test_unrecorded_is_distinct_from_explicitly_undeclared(self):
        run = publisher()
        selected = {"repository": "PerishLab/library", "marker": "v1.2.3-rc.1"}
        held = dict(selected, state="complete", commit="b" * 40, tree="c" * 40, completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, media={"binaries": "none"})
        self.assertEqual(admission.publication(run, selected, held)["state"], "unrecorded")
        self.assertEqual(admission.publication(run, selected, dict(held, trial=None))["state"], "undeclared")


    def test_canonical_reader_schema_digest_and_errors_refuse(self):
        raw = b"[windtun]"
        reply = {"schema": "plumb.windtun.declaration/v1", "manifest": hashlib.sha256(raw).hexdigest(), "windtun": self.specification()}
        for change in ({"schema": "unknown"}, {"manifest": "f" * 64}, {"extra": True}, {"windtun": dict(self.specification(), cases=[])}):
            with self.subTest(change=change), mock.patch.object(declaration.subprocess, "check_output", return_value=json.dumps(dict(reply, **change))), self.assertRaises(Refusal):
                declaration.inspected("/source", raw)
        with mock.patch.object(declaration.subprocess, "check_output", side_effect=OSError("unavailable")), self.assertRaises(Refusal):
            declaration.inspected("/source", raw)

    def test_ready_context_does_not_wait_for_whole_workflow_or_parse_title(self):
        run = dict(publisher(), status="in_progress", display_title="not an identity protocol")
        identity = {"repository": "PerishLab/keel", "marker": "v1.2.3-rc.1", "commit": "b" * 40, "tree": "c" * 40}
        selected = admission.candidate(run, "7", identity)
        self.assertEqual(selected, identity)
        trial = {"schema": declaration.SCHEMA, "source": identity, "manifest": "d" * 64, "authority": "https://releases.keel.perish.uk", "specification": self.specification()}
        record = dict(identity, state="complete", completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, media={"binaries": "published"}, trial=trial)
        self.assertTrue(admission.publication(run, selected, record)["eligible"])
        for change in ({"state": "incomplete"}, {"commit": "f" * 40}, {"completed": dict(record["completed"], attempt="2")}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                admission.publication(run, selected, dict(record, **change))

    def test_replay_reads_exact_completed_publication_attempt(self):
        run = dict(publisher(), run_attempt=3)
        record = {"completed": {"run": "7", "attempt": "1", "wharf": "a" * 40}}
        with mock.patch.object(admission.subprocess, "check_output", return_value=json.dumps(publisher())) as provider:
            self.assertEqual(admission.completed(run, "7", record)["run_attempt"], 1)
            self.assertTrue(provider.call_args.args[0][-1].endswith("/runs/7/attempts/1"))
        with mock.patch.object(admission.subprocess, "check_output") as provider:
            self.assertEqual(admission.completed(publisher(), "7", record), publisher())
            provider.assert_not_called()
        with self.assertRaises(Refusal):
            admission.completed(run, "7", {"completed": {"run": "7", "attempt": "../2"}})

    def test_replay_cannot_test_another_product_than_requested(self):
        selected = {"repository": "PerishLab/keel", "marker": "v1.2.3-rc.1"}
        admission.selection(selected, json.dumps(selected))
        admission.selection(selected, "null")
        for expected in (dict(selected, repository="PerishLab/plumb"), dict(selected, marker="v1.2.4-rc.1"), {"repository": selected["repository"]}, dict(selected, extra=True)):
            with self.subTest(expected=expected), self.assertRaises(Refusal):
                admission.selection(selected, json.dumps(expected))
        with self.assertRaises(Refusal):
            admission.selection(None, json.dumps(selected))
        with self.assertRaises(Refusal):
            admission.selection(selected, "{")
