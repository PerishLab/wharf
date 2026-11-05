import unittest

from tests.promotion.preparation.test_workflow import condition
from tests.promotion.test_workflow import jobs


class Workflow(unittest.TestCase):
    def test_distribution_receives_the_plan_reference_and_workload_reader(self):
        source = jobs()
        self.assertIn("plan-reference: ${{ steps.plan.outputs.plan-reference }}", source["plan"])
        self.assertIn("WHARF_NEEDS: ${{ toJSON(needs) }}", source["distribution"])
        self.assertIn("WHARF_R2_ACCESS_KEY_ID: ${{ secrets.WHARF_R2_ACCESS_KEY_ID }}", source["distribution"])
        self.assertIn("WHARF_R2_SECRET_ACCESS_KEY: ${{ secrets.WHARF_R2_SECRET_ACCESS_KEY }}", source["distribution"])

    def test_linux_comparison_can_read_native_provenance_without_storage_writers(self):
        source = jobs()["build-proof"]
        reader = source.split('- name: "[compare] published binary"', 1)[1].split('- name:', 1)[0]
        self.assertIn("GH_TOKEN: ${{ github.token }}", reader)
        self.assertIn("actions: read", source)
        self.assertNotIn("ACCESS_KEY", reader)
        self.assertNotIn("SECRET", reader)

    def test_producer_is_post_distribution_and_reader_holds_no_writers(self):
        source = jobs()["native-proof"]
        self.assertIn("needs: [request, plan, distribution]", source)
        self.assertIn("needs.distribution.outputs.state == 'complete'", source)
        self.assertIn("prepare: rcodesign", source)
        reader = source.split('- name: "[read] native binary"', 1)[1].split('- name:', 1)[0]
        self.assertIn("scripts.distribution native-read", reader)
        self.assertNotIn("ACCESS_KEY", reader)
        self.assertNotIn("SECRET", reader)
        self.assertNotIn("uses: ./wharf/.github/actions/product", source)

    def test_candidate_waits_for_linux_and_has_no_release_writer(self):
        source = jobs()["native-candidate"]
        self.assertIn("needs: [request, plan, prepared, build-candidate]", source)
        self.assertIn("needs.build-candidate.result == 'success'", source)
        self.assertIn("scripts.distribution native-candidate-read", source)
        self.assertNotIn("RELEASES", source)
        self.assertNotIn("WHARF_APP_PRIVATE_KEY", source)

    def test_observers_do_not_activate_publication_gates(self):
        for name in ("release", "cargo", "npm", "oci", "chart", "cfworker", "channel"):
            source = jobs()[name]
            dependencies = next(line for line in source.splitlines() if line.startswith("    needs:"))
            with self.subTest(job=name):
                self.assertNotIn("native-proof", dependencies)
                self.assertNotIn("native-candidate", dependencies)


    def test_failure_observations_are_preserved_without_a_false_comparison_failure(self):
        source = jobs()["native-proof"]
        self.assertEqual(source.count("WHARF_OBSERVATION: ${{ runner.temp }}/native-observation"), 3)
        comparison = source.split('- name: "[preserve] native comparison"', 1)[1].split('- name:', 1)[0]
        self.assertIn("if-no-files-found: ignore", comparison)
        observation = source.split('- name: "[preserve] native observation"', 1)[1]
        self.assertIn("if: always() && steps.proved.outcome != 'skipped'", observation)
        self.assertIn("name: native-observation-${{ github.run_attempt }}", observation)
        self.assertIn("if-no-files-found: error", observation)
        self.assertNotIn("SECRET", observation)

    def test_replay_roles_follow_the_requested_testing_domain(self):
        document = jobs()
        outcomes = {name: "skipped" for name in document} | {"request": "success"}
        selected = {"prepare": False, "candidate": False, "run": "7", "reference": ""}
        for reference in ("", "v1.2.3-rc.1"):
            selected["reference"] = reference
            for name in ("build-proof", "native-proof"):
                with self.subTest(reference=reference, job=name):
                    self.assertEqual(condition(document[name], selected, outcomes), bool(reference))
            self.assertEqual(condition(document["windtun-prepare"], selected, outcomes), not reference)
        selected["reference"] = ""
        outcomes = {name: "success" for name in document}
        for name in ("build-proof", "native-proof"):
            self.assertFalse(condition(document[name], selected, outcomes))

    def test_publication_keeps_proof_roles_and_their_admission(self):
        document = jobs()
        outcomes = {name: "success" for name in document}
        selected = {"prepare": False, "candidate": False, "run": "", "reference": ""}
        for name in ("build-proof", "native-proof"):
            source = document[name]
            for reference in ("", "v1.2.3-rc.1"):
                selected["reference"] = reference
                self.assertTrue(condition(source, selected, outcomes))
                self.assertFalse(condition(source.replace("state == 'complete'", "state == 'pending'"), selected, outcomes))
                self.assertFalse(condition(source.replace("binaryproof == 'true'", "binaryproof == 'false'"), selected, outcomes))
            for rejected in ("failure", "skipped", "cancelled"):
                self.assertFalse(condition(source, selected, outcomes | {"request": rejected}))

    def test_explicit_proof_replays_nonstable_without_windtun_or_publication(self):
        document = jobs()
        outcomes = {name: "skipped" for name in document} | {"request": "success"}
        selected = {"prepare": False, "candidate": False, "proof": True, "run": "7", "reference": "", "repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"}
        for name in ("build-proof", "native-proof"):
            self.assertTrue(condition(document[name], selected, outcomes))
        for name in ("plan", "release", "cargo", "npm", "oci", "chart", "cfworker", "channel", "distribution", "windtun-prepare", "windtun-issue"):
            with self.subTest(job=name):
                self.assertFalse(condition(document[name], selected, outcomes))
        selected["reference"] = "v1.2.3-rc.1"
        selected["marker"] = "v1.2.3"
        for name in ("build-proof", "native-proof"):
            self.assertTrue(condition(document[name], selected, outcomes))
        self.assertFalse(condition(document["windtun-prepare"], selected, outcomes))
