import unittest

from tests.promotion.test_workflow import jobs


class Workflow(unittest.TestCase):
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
