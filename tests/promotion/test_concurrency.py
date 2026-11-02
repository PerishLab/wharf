import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from lib.content import marker
from lib.process import git


def scheduling():
    source = (Path(git('.', 'rev-parse', '--show-toplevel')) / '.github/workflows/ship.yml').read_text()
    return source.split('\nconcurrency:\n', 1)[1].split('\nenv:', 1)[0]


def group(**selected):
    values = dict(repository='PerishLab/plumb', marker='v1.2.3', prepare=False, candidate=False, run='', reference='', case='declared', attempt='1') | selected
    template = next(line.split('group:', 1)[1].strip() for line in scheduling().splitlines() if line.startswith('  group:'))

    def expression(match):
        text = match[1].strip().replace('&&', ' and ').replace('||', ' or ')
        text = re.sub(r'!(?!=)', 'not ', text).strip()
        return str(eval(text, {'__builtins__': {}}, {'inputs': SimpleNamespace(**values), 'contains': lambda value, part: part in value}))

    return re.sub(r'\$\{\{(.*?)\}\}', expression, template).casefold()


class Concurrency(unittest.TestCase):
    def test_ordinary_stable_markers_share_one_product_group(self):
        markers = ('v0.0.0', 'v1.2.3', 'v23.45.678', 'v01.002.3')
        self.assertTrue(all(marker.channel(value) == 'stable' for value in markers))
        self.assertEqual(len({group(marker=value) for value in markers}), 1)

    def test_distinct_products_do_not_block_each_other(self):
        self.assertNotEqual(group(repository='PerishLab/plumb'), group(repository='PerishLab/runseal'))

    def test_each_nonstable_marker_keeps_its_existing_group(self):
        markers = ('v1.2.3-alpha.1', 'v1.2.3-beta.17', 'v1.2.3-rc.12', 'v1.2.4-rc.1')
        self.assertTrue(all(marker.channel(value) != 'stable' for value in markers))
        for value in markers:
            with self.subTest(marker=value):
                self.assertEqual(group(marker=value), f'ship-perishlab/plumb-{value}')
                self.assertNotEqual(group(marker=value), group())
        self.assertEqual(len({group(marker=value) for value in markers}), len(markers))

    def test_prepare_candidate_and_replay_do_not_take_the_publication_group(self):
        modes = ({'prepare': True}, {'candidate': True, 'run': '7'}, {'run': '7'}, {'run': '7', 'reference': 'v1.2.3-rc.1'}, {'run': '7', 'case': 'context'})
        for mode in modes:
            with self.subTest(mode=mode):
                self.assertEqual(group(**mode), 'ship-perishlab/plumb-v1.2.3')
                self.assertNotEqual(group(**mode), group())
        self.assertNotEqual(group(prepare=True, marker='v1.2.3'), group(prepare=True, marker='v1.2.4'))

    def test_other_inputs_cannot_let_ordinary_publishers_escape_the_group(self):
        for changed in ({'reference': 'v1.2.3-rc.1'}, {'case': 'context'}, {'attempt': '2'}):
            with self.subTest(changed=changed):
                self.assertEqual(group(**changed), group())

    def test_replay_of_a_nonstable_marker_preserves_its_existing_isolation(self):
        self.assertEqual(group(marker='v1.2.3-rc.1', run='7'), group(marker='v1.2.3-rc.1'))

    def test_group_spans_the_workflow_and_retains_waiting_runs_without_cancelling_active_work(self):
        block = scheduling()
        self.assertIn('  queue: max', block)
        self.assertIn('  cancel-in-progress: false', block)
        source = (Path(git('.', 'rev-parse', '--show-toplevel')) / '.github/workflows/ship.yml').read_text()
        self.assertEqual(len(re.findall(r'^\s*concurrency:', source, re.MULTILINE)), 1)
