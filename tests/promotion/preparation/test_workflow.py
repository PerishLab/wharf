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
    if selected.get("semantics"):
        mode = {"capture": "semantics", "adoption": "adoption", "world": "world", "retain": "retention", "history": "history", "transition": "transition", "selection": "selection", "original": "original"}[json.loads(selected["semantics"])["kind"]]
    text = text.replace("needs.request.outputs.mode", repr(mode))
    text = re.sub(r"needs\.windtun-prepare\.outputs\.ready", repr("true"), text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", "not ", text)
    return bool(eval(text, {"__builtins__": {}}, {"inputs": SimpleNamespace(**({"proof": False, "semantics": ""} | selected)), "always": lambda: True, "cancelled": lambda: False, "contains": lambda held, value: value in held, "format": lambda pattern, *values: pattern.format(*values)}))


class Workflow(unittest.TestCase):
    def test_original_source_has_only_its_metadata_role_and_separate_credentials(self):
        document = jobs()
        selected = {"semantics": '{"kind":"original"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v0.143.0"}
        outcomes = {name: "success" for name in document}
        self.assertTrue(condition(document["semantics-original"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-original"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-original"]
        prepare = source.split('      - name: "[read] original source closure"', 1)[1].split('      - name:', 1)[0]
        publish = source.split('      - name: "[retain] original source closure"', 1)[1].split('      - name:', 1)[0]
        confirm = source.split('      - name: "[retain] original $', 1)[1].split('      - name:', 1)[0]
        self.assertIn("WHARF_R2_SECRET_ACCESS_KEY", prepare)
        self.assertNotIn("WHARF_RELEASES_", prepare)
        self.assertIn("WHARF_RELEASES_SECRET_ACCESS_KEY", publish)
        self.assertNotIn("WHARF_R2_", publish)
        self.assertNotIn("WHARF_R2_", confirm)
        self.assertNotIn("WHARF_RELEASES_", confirm)
        for forbidden in ("[checkout] product", "packages: write", "scripts.ship", "prepare: rust", "prepare: snapshot"):
            self.assertNotIn(forbidden, source)

    def test_selection_excludes_publication_roles_and_refreshes_each_read_phase(self):
        document = jobs()
        selected = {"semantics": '{"kind":"selection"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v0.143.0"}
        outcomes = {name: "success" for name in document}
        self.assertTrue(condition(document["semantics-select"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-select"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-select"]
        self.assertEqual(source.count("uses: actions/create-github-app-token@"), 3)
        self.assertEqual(source.count("permission-actions: read"), 3)
        self.assertEqual(source.count("permission-contents: read"), 3)
        preparation = source.split('      - name: "[prepare] exact family materials"', 1)[1].split('      - name:', 1)[0]
        publication = source.split('      - name: "[select] exact family"', 1)[1].split('      - name:', 1)[0]
        confirmation = source.split('      - name: "[select] family ', 1)[1].split('      - name:', 1)[0]
        for block in (preparation, confirmation):
            for forbidden in ("WHARF_R2_", "WHARF_RELEASES_", "WHARF_APP_PRIVATE_KEY"):
                self.assertNotIn(forbidden, block)
        self.assertIn("steps.metadata.outputs.token", preparation)
        self.assertIn("steps.publication.outputs.token", publication)
        self.assertIn("steps.confirmation.outputs.token", confirmation)
        self.assertIn("WHARF_RELEASES_SECRET_ACCESS_KEY", publication)
        for forbidden in ("packages: write", "permission-contents: write", "WHARF_R2_", "scripts.ship", "prepare: rust"):
            self.assertNotIn(forbidden, source)

    def test_transition_has_one_metadata_issuer_and_no_publication_role(self):
        document = jobs()
        selected = {"semantics": '{"kind":"transition"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v0.141.0"}
        outcomes = {name: "success" for name in document}
        self.assertTrue(condition(document["semantics-issue"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-issue"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-issue"]
        self.assertIn("scripts.distribution semantics-transition", source)
        self.assertIn("if: always()", source)
        confirmation = source.split('      - name: "[issue] semantics ', 1)[1]
        for forbidden in ("WHARF_R2_", "WHARF_RELEASES_", "WHARF_APP_PRIVATE_KEY"):
            self.assertNotIn(forbidden, confirmation)

    def test_history_can_only_write_metadata_and_confirmation_has_no_writer(self):
        document = jobs()
        selected = {"semantics": '{"kind":"history"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v0.140.0"}
        outcomes = {name: "success" for name in document}
        self.assertTrue(condition(document["semantics-history"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-history"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-history"]
        for forbidden in ("packages: write", "uses: ./wharf/.github/actions/product", "scripts.ship", "WHARF_R2_ACCESS_KEY_ID", "prepare: rust"):
            self.assertNotIn(forbidden, source)
        self.assertIn("permission-actions: read", source)
        self.assertIn("permission-contents: read", source)
        self.assertIn('name: "[retain] history ${{ steps.checkpoint.outputs.digest }}"', source)
        confirmation = source.split('      - name: "[retain] history ', 1)[1]
        for forbidden in ("WHARF_R2_", "WHARF_RELEASES_", "WHARF_APP_PRIVATE_KEY"):
            self.assertNotIn(forbidden, confirmation)

    def test_retention_has_only_metadata_writer_and_no_product_execution(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        selected = {"semantics": '{"kind":"retain"}', "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"}
        self.assertTrue(condition(document["semantics-retain"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-retain"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-retain"]
        for forbidden in ("packages: write", "packages: read", "WHARF_APP_PRIVATE_KEY", "steps.selected.outputs.commit", "scripts.ship", "prepare: rust", "prepare: snapshot", "cargo ", "engines"):
            self.assertNotIn(forbidden, source)
        self.assertIn("semantics-retain-confirm", source)
        confirmation = source.split('      - name: "[retain] world ', 1)[1]
        for forbidden in ("WHARF_R2_", "WHARF_RELEASES_"):
            self.assertNotIn(forbidden, confirmation)

    def test_prospective_world_has_no_writer_or_ordinary_publication_role(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        requested = {"kind": "world", "schema": "wharf.release.semantics.request/v1", "commit": "a" * 40, "scenes": {"fixture": "selected"}}
        selected = {"semantics": json.dumps(requested), "prepare": False, "candidate": False, "proof": False, "run": "", "reference": "", "repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"}
        self.assertTrue(condition(document["semantics-world"], selected, outcomes))
        for name in document.keys() - {"request", "semantics-world"}:
            with self.subTest(name=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        source = document["semantics-world"]
        for forbidden in ("RELEASES", "WHARF_R2_", "packages: write", "WHARF_APP_PRIVATE_KEY", "scripts.ship", "docker login"):
            self.assertNotIn(forbidden, source)
        self.assertIn("packages: read", source)
        self.assertIn("ref: ${{ steps.selected.outputs.commit }}", source)
        self.assertIn('name: "[prepare] world ${{ steps.world.outputs.digest }} ${{ steps.world.outputs.payload }}"', source)
        self.assertIn("semantics-world-confirm", source)

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
        self.assertIn('name: "[issue] semantics ${{ steps.adoption.outputs.digest || steps.transition.outputs.digest }}"', source)
        self.assertIn("semantics-confirm", source)
        for forbidden in ("packages: write", "uses: ./wharf/.github/actions/product", "scripts.ship"):
            self.assertNotIn(forbidden, source)
        self.assertIn("permission-actions: read", source)
        self.assertIn("permission-contents: read", source)
        issue = source.split('      - name: "[issue] adoption record"', 1)[1]
        self.assertIn("GH_TOKEN: ${{ steps.metadata.outputs.token }}", issue)
        self.assertNotIn("WHARF_APP_PRIVATE_KEY", issue)
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
