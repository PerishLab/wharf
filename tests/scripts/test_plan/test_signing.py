import unittest
from unittest import mock

from scripts import plan


class Signing(unittest.TestCase):
    def entries(self, targets):
        result = {}
        for target in targets:
            result['binary-' + target] = {'key': 'a' * 64, 'decision': 'reuse'}
            result['bind-' + target] = {'key': 'b' * 64, 'decision': 'run', 'consumes': ['binary-' + target]}
        return result

    def test_linux_only_binding_does_not_prepare_unused_signer(self):
        layers = plan.units(self.entries(['linux']))
        binding = next(item for items in layers.values() for item in items if item['action'] == 'bind')
        self.assertEqual(binding['prepare'], [])

    def test_macos_binding_keeps_pinned_signer_with_other_platforms(self):
        layers = plan.units(self.entries(['linux', 'macos', 'windows']))
        binding = next(item for items in layers.values() for item in items if item['action'] == 'bind')
        self.assertEqual(binding['prepare'], ['rcodesign'])

    def test_tools_follow_plan_targets_even_when_bound_material_is_reused(self):
        for targets, expected in [(['linux'], ''), (['linux', 'macos'], 'rcodesign')]:
            entries = self.entries(targets)
            for value in entries.values():
                value['decision'] = 'reuse'
            with self.subTest(targets=targets), mock.patch.object(plan.parameters, 'answer'):
                self.assertEqual(plan.emit(entries, '1')['signer'], expected)

    def test_unscoped_unit_retains_conservative_existing_preparation(self):
        self.assertEqual(plan.unit('bind-linux')['prepare'], ['rcodesign'])
