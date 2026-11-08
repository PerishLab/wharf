import itertools
from pathlib import Path
import re
import unittest

from lib.process import git


def jobs():
    source = (Path(git('.', 'rev-parse', '--show-toplevel')) / '.github/workflows/ship.yml').read_text()
    found = {}
    current = None
    for line in source.split('jobs:\n', 1)[1].splitlines():
        if re.fullmatch(r'  [a-z0-9-]+:', line):
            current = line.strip().rstrip(':')
            found[current] = []
        if current:
            found[current].append(line)
    return {name: '\n'.join(lines) for name, lines in found.items()}


class Workflow(unittest.TestCase):
    def test_ordinary_pilot_waits_for_builds_and_never_holds_product_writers(self):
        document = jobs()
        prepare = document['promotion-prepare']
        self.assertIn('needs: [plan, layer-1, layer-2, layer-3, layer-4]', prepare)
        self.assertIn('always() && !cancelled()', prepare)
        self.assertIn("inputs.repository == 'PerishLab/plumb'", prepare)
        self.assertIn("!contains(inputs.marker, '-')", prepare)
        self.assertIn('ref: ' + chr(36) + '{{ github.sha }}', prepare)
        self.assertIn('scripts.distribution promotion-prepare', prepare)
        self.assertIn('scripts.distribution promotion-read', prepare)
        self.assertIn('WHARF_R2_ACCESS_KEY_ID', prepare)
        self.assertNotIn('RELEASES', prepare)
        self.assertNotIn('WHARF_APP_PRIVATE_KEY', prepare)
        self.assertNotIn('uses: ./wharf/.github/actions/product', prepare)
        self.assertNotIn('packages: write', prepare)
        self.assertIn('env PLUMB_CHANNEL=stable PLUMB_VERSION= sh', prepare)

    def test_each_publisher_waits_but_only_original_build_results_control_admission(self):
        document = jobs()
        expression = "format('{0} {1} {2} {3}', needs.layer-1.result, needs.layer-2.result, needs.layer-3.result, needs.layer-4.result)"
        for job in ['npm', 'oci', 'chart', 'release', 'cfworker', 'cargo']:
            source = document[job]
            with self.subTest(job=job):
                self.assertIn('needs: [plan, layer-1, layer-2, layer-3, layer-4, promotion-prepare]', source)
                condition = next(line for line in source.splitlines() if line.startswith('    if:'))
                self.assertIn("!cancelled()", condition)
                self.assertIn("needs.plan.result == 'success'", condition)
                self.assertIn(f"!contains({expression}, 'failure')", condition)
                self.assertIn(f"!contains({expression}, 'cancelled')", condition)
                self.assertNotIn('needs.*.result', condition)
                self.assertNotIn('promotion-prepare.result', condition)
        for results in itertools.product(['success', 'skipped', 'failure', 'cancelled'], repeat=4):
            original = not any(value in ('failure', 'cancelled') for value in results)
            joined = ' '.join(results)
            self.assertEqual(original, 'failure' not in joined and 'cancelled' not in joined)

    def test_inspection_and_nonstable_default_execution_keep_their_separate_conditions(self):
        document = jobs()
        self.assertIn("(inputs.candidate || (inputs.prepare && needs.prepared.result == 'success'))", document['build-candidate'])
        self.assertNotIn('promotion-prepare', next(line for line in document['windtun-prepare'].splitlines() if line.startswith('    needs:')))

    def test_channel_still_requires_all_original_publication_results(self):
        source = jobs()['channel']
        condition = next(line for line in source.splitlines() if line.startswith('    if:'))
        self.assertIn("!contains(needs.*.result, 'failure')", condition)
        self.assertIn("!contains(needs.*.result, 'cancelled')", condition)
        self.assertNotIn('needs.layer-', condition)

    def test_every_workflow_need_reference_names_an_explicit_dependency(self):
        for name, source in jobs().items():
            declared = next((line for line in source.splitlines() if line.startswith('    needs:')), '')
            dependencies = set(re.findall(r'[a-z0-9-]+', declared.split(':', 1)[-1]))
            references = set(re.findall(r'needs\.([a-z0-9-]+)\.', source))
            self.assertLessEqual(references, dependencies, name)

    def test_whole_workflow_cancellation_stops_preparation_and_all_writers(self):
        document = jobs()
        for name in ['promotion-prepare', 'npm', 'oci', 'chart', 'release', 'cfworker', 'cargo', 'channel']:
            condition = next(line for line in document[name].splitlines() if line.startswith('    if:'))
            self.assertIn('always() && !cancelled()', condition)
