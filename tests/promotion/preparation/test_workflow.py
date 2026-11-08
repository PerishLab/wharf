import re
import json
import unittest
from types import SimpleNamespace

from tests.promotion.test_workflow import jobs

WRITERS = {"npm", "oci", "chart", "release", "channel", "cfworker", "cargo", "distribution", "build-proof", "native-proof", "windtun-prepare", "windtun-issue"}


def condition(source, selected, outcomes):
    text = next(line.split("if:", 1)[1].strip() for line in source.splitlines() if line.startswith("    if:"))
    text = text.removeprefix("${{").removesuffix("}}").strip()
    dependencies = re.findall(r"[a-z0-9-]+", next((line.split(":", 1)[1] for line in source.splitlines() if line.startswith("    needs:")), ""))
    results = [outcomes[name] for name in dependencies]
    if not re.search(r"always\(|cancelled\(", text) and any(value != "success" for value in results):
        return False
    text = text.replace("needs.*.result", repr(results))
    text = re.sub(r"needs\.([a-z0-9-]+)\.result", lambda match: repr(outcomes[match[1]]), text)
    text = re.sub(r"needs\.plan\.outputs\.([a-z0-9-]+)", lambda match: repr("complete" if match[1] == "state" else "true" if match[1] in ("binaryproof", "windtun") else "[{}]" if match[1].startswith("layer-") else "run"), text)
    text = text.replace("needs.distribution.outputs.state", repr("complete"))
    mode = "semantics" if selected.get("semantics") else "prepare" if selected.get("prepare") else "ordinary"
    if selected.get("semantics") and json.loads(selected["semantics"])["kind"] == "adoption":
        mode = "adoption"
    text = text.replace("needs.request.outputs.mode", repr(mode))
    text = re.sub(r"needs\.windtun-prepare\.outputs\.ready", repr("true"), text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", "not ", text)
    return bool(eval(text, {"__builtins__": {}}, {"inputs": SimpleNamespace(**({"proof": False, "semantics": ""} | selected)), "always": lambda: True, "cancelled": lambda: False, "contains": lambda held, value: value in held, "format": lambda pattern, *values: pattern.format(*values)}))


class Workflow(unittest.TestCase):
    def test_adoption_can_only_issue_metadata_and_confirmation_has_no_writer(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        requested = {"kind": "adoption", "schema": "wharf.release.semantics.request/v1", "scenes": {"fixture": "selected"}, "review": "native-review"}
        selected = {"semantics": json.dumps(requested), "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v1.2.3"}
        self.assertTrue(condition(document["semantics-issue"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-issue"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-issue"]
        self.assertIn('name: "[issue] version semantics"', source)
        self.assertIn('name: "[issue] semantics ${{ steps.adoption.outputs.digest }}"', source)
        self.assertIn("semantics-confirm", source)
        for forbidden in ("packages: write", "WHARF_APP_PRIVATE_KEY", "uses: ./wharf/.github/actions/product", "scripts.ship"):
            self.assertNotIn(forbidden, source)
        confirmation = source.split('      - name: "[issue] semantics ', 1)[1]
        for forbidden in ("WHARF_RELEASES_ACCESS_KEY_ID", "WHARF_R2_ACCESS_KEY_ID", "WHARF_RELEASES_SECRET_ACCESS_KEY", "WHARF_R2_SECRET_ACCESS_KEY"):
            self.assertNotIn(forbidden, confirmation)

    def test_semantics_material_capture_cannot_start_other_roles(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        selected = {"semantics": '{"kind":"capture","schema":"wharf.release.semantics.request/v1"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v1.2.3"}
        self.assertTrue(condition(document["semantics"], selected, outcomes))
        for name in document.keys() - {"request", "semantics"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        for forbidden in ("RELEASES", "packages: write", "WHARF_APP_PRIVATE_KEY", "uses: ./wharf/.github/actions/product", "scripts.ship"):
            self.assertNotIn(forbidden, document["semantics"])
        self.assertIn("semantics-capture", document["semantics"])

    def test_preparation_excludes_every_writer_even_with_publication_outputs(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        selected = {"prepare": True, "candidate": False, "run": "", "repository": "PerishLab/plumb", "marker": "v1.2.3", "reference": "v1.2.3-rc.1"}
        for name in WRITERS | {"promotion-prepare"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        for name in ("plan", "layer-1", "layer-2", "layer-3", "layer-4", "prepared", "build-candidate", "native-candidate"):
            with self.subTest(name=name):
                self.assertTrue(condition(document[name], selected, outcomes))
        outcomes["prepared"] = "failure"
        self.assertFalse(condition(document["build-candidate"], selected, outcomes))

    def test_invalid_request_stops_replay_writers_and_manual_inspectors(self):
        document = jobs()
        outcomes = {name: "skipped" for name in document} | {"request": "failure"}
        for prepare, candidate, run in ((True, True, "7"), (False, False, "7"), (False, True, "7")):
            selected = {"prepare": prepare, "candidate": candidate, "run": run, "repository": "PerishLab/concord", "marker": "v1.2.3", "reference": ""}
            for name in WRITERS | {"plan", "prepared", "build-candidate", "native-candidate"}:
                with self.subTest(name=name, selected=selected):
                    self.assertFalse(condition(document[name], selected, outcomes))

    def test_manual_inspection_keeps_skipped_plan_and_prepared_ancestors(self):
        document = jobs()
        outcomes = {name: "skipped" for name in document} | {"request": "success", "build-candidate": "success"}
        selected = {"prepare": False, "candidate": True, "run": "7"}
        self.assertTrue(condition(document["build-candidate"], selected, outcomes))
        self.assertTrue(condition(document["native-candidate"], selected, outcomes))

    def test_read_only_admission_and_prepared_jobs_have_no_product_writer(self):
        document = jobs()
        self.assertTrue(document["request"].startswith("  request:"))
        for name in ("request", "prepared"):
            source = document[name]
            for forbidden in ("WHARF_APP_PRIVATE_KEY", "RELEASES", "packages: write", "uses: ./wharf/.github/actions/product"):
                self.assertNotIn(forbidden, source)
        self.assertNotIn("WHARF_R2_ACCESS_KEY_ID", document["request"])
        self.assertIn("scripts.distribution prepared-read", document["prepared"])
        self.assertIn("needs: [request]", document["plan"])
