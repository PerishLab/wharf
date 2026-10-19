import ast
import re
import unittest
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.process.lane import execution
from tests.lib.preview.test_admission import CONTEXT


def workflow(name):
    root = Path(execution.__file__).resolve().parents[3]
    return (root / '.github/workflows' / (name + '.yml')).read_text()


def jobs(name):
    fields = re.split(r'(?m)^  ([a-z]+):\n', workflow(name).split('\njobs:\n', 1)[1])
    return dict(zip(fields[1::2], fields[2::2]))


def field(body, key):
    found = re.search(r'^    ' + key + r': (.+)$', body, re.MULTILINE)
    if found is None:
        return None
    value = found.group(1)
    return value[1:-1] if value[0] in "'\"" and value[-1] == value[0] else value


def admitted(body, results):
    expression = field(body, 'if')
    if expression is None:
        return True
    expression = expression.replace('always()', 'True').replace('&&', ' and ').replace('||', ' or ')
    tree = ast.parse(expression, mode='eval')
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Attribute, ast.Name, ast.Load, ast.Constant)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise AssertionError('workflow condition is outside the bounded test expression subset')
    needs = SimpleNamespace(**{name: SimpleNamespace(result=value['result'], outputs=SimpleNamespace(**value.get('outputs', {}))) for name, value in results.items()})
    return bool(eval(compile(tree, '<workflow-condition>', 'eval'), {'__builtins__': {}}, {'needs': needs}))


def original_jobs(action):
    caller = jobs('lane')
    intake = jobs('intake')
    results = {'admission': {'result': 'success', 'outputs': {'operation': action}}}
    projected = []
    for name, body in intake.items():
        conclusion = 'success' if admitted(body, results) else 'skipped'
        results[name] = dict(results.get(name, {}), result=conclusion)
        projected.append((field(caller['intake'], 'name') + ' / ' + field(body, 'name'), conclusion))
    for name, unit in (('observe', 'observe'), ('apply', 'deploy'), ('dispose', 'dispose'), ('evidence', 'evidence')):
        children = jobs(unit)
        selected = {'intake': {'result': 'success', 'outputs': {'operation': action}}, 'observe': {'result': 'success'},
                    'apply': {'result': 'success' if action == 'apply' else 'skipped'},
                    'dispose': {'result': 'success' if action == 'dispose' else 'skipped'}}
        conclusion = 'success' if admitted(caller[name], selected) else 'skipped'
        for child, body in children.items():
            conclusion = None if child == 'state' else conclusion
            projected.append((field(caller[name], 'name') + ' / ' + field(body, 'name'), conclusion))
    return [{'id': index + 1, 'name': name, 'run_id': CONTEXT['run'], 'run_attempt': CONTEXT['attempt'], 'head_sha': CONTEXT['commit'],
             'status': 'in_progress' if conclusion is None else 'completed', 'conclusion': conclusion} for index, (name, conclusion) in enumerate(projected)]


class Graph(unittest.TestCase):
    def test_singleword_workflow_paths_keep_exact_internal_caller_mapping(self):
        root = Path(execution.__file__).resolve().parents[3]
        directory = root / '.github/workflows'
        self.assertTrue(all(re.fullmatch(r'[a-z]+\.yml', path.name) for path in directory.iterdir()))
        self.assertEqual(list(directory.glob('lane-*.yml')), [])
        caller = jobs('lane')
        mapping = {'intake': 'intake', 'observe': 'observe', 'apply': 'deploy', 'dispose': 'dispose', 'evidence': 'evidence'}
        for job, unit in mapping.items():
            self.assertEqual(field(caller[job], 'uses'), './.github/workflows/' + unit + '.yml')
            body = workflow(unit)
            self.assertIn('workflow_call:', body)
            for trigger in ('workflow_dispatch:', 'repository_dispatch:', 'push:', 'pull_request:', 'schedule:'):
                self.assertNotIn(trigger, body)

    def test_fixed_main_dispatch_is_single_serialized_exact_request_entry(self):
        body = workflow('lane')
        self.assertIn('workflow_dispatch:', body)
        self.assertEqual(re.findall(r'^      ([a-z]+):$', body, re.MULTILINE), ['request'])
        self.assertIn("if: github.ref == 'refs/heads/main' && github.repository == 'PerishLab/wharf'", body)
        self.assertIn('group: wharf-static-lane\n  cancel-in-progress: false', body)
        self.assertEqual(set(jobs('lane')), {'intake', 'observe', 'apply', 'dispose', 'evidence'})
        for forbidden in ('repository_dispatch:', 'push:', 'pull_request:', 'schedule:', 'secrets: inherit', 'release', 'ship', 'artifact', 'docker'):
            self.assertNotIn(forbidden, body)

    def test_all_consumed_producers_are_direct_needs_with_separate_provider_writers(self):
        caller = jobs('lane')
        self.assertEqual(field(caller['observe'], 'needs'), '[intake]')
        for name in ('apply', 'dispose'):
            self.assertEqual(field(caller[name], 'needs'), '[intake, observe]')
        self.assertEqual(field(caller['evidence'], 'needs'), '[intake, observe, apply, dispose]')
        self.assertIn('WHARF_LANE_DEPLOYMENT_TOKEN:', caller['apply'])
        self.assertNotIn('WHARF_LANE_DISPOSAL_TOKEN:', caller['apply'])
        self.assertIn('WHARF_LANE_DISPOSAL_TOKEN:', caller['dispose'])
        self.assertNotIn('WHARF_LANE_DEPLOYMENT_TOKEN:', caller['dispose'])
        for name in ('observe', 'evidence'):
            for forbidden in ('WHARF_LANE_DEPLOYMENT_TOKEN:', 'WHARF_LANE_DISPOSAL_TOKEN:', 'WHARF_R2_SECRET_ACCESS_KEY:'):
                self.assertNotIn(forbidden, caller[name])
        self.assertIn('needs.observe.outputs.provider', caller['apply'])
        self.assertIn('needs.observe.outputs.provider', caller['dispose'])
        for selected in ('needs.apply.outputs.result', 'needs.dispose.outputs.result', 'needs.observe.outputs.provider'):
            self.assertIn(selected, caller['evidence'])

    def test_workflow_derived_native_job_projection_agrees_for_all_three_actions(self):
        for action in ('apply', 'dispose', 'inspect'):
            values = original_jobs(action)
            api = SimpleNamespace(jobs=mock.Mock(return_value={'total_count': len(values), 'jobs': values}))
            with self.subTest(action=action):
                execution.native_jobs({'operation': action}, CONTEXT, api)
            consumer = next(value for value in values if value['name'] == '[verify] request lane / [verify] product lane')
            self.assertEqual(consumer['conclusion'], 'success')
            api.jobs.assert_called_once_with(CONTEXT['run'], CONTEXT['attempt'])

    def test_failure_cancellation_and_skipped_producers_cannot_start_provider_or_evidence(self):
        caller = jobs('lane')
        outcomes = ('success', 'failure', 'cancelled', 'skipped', 'timed_out')
        for action, intake, observe, apply, dispose in product(('apply', 'dispose', 'inspect'), outcomes, outcomes, outcomes, outcomes):
            results = {'intake': {'result': intake, 'outputs': {'operation': action}}, 'observe': {'result': observe},
                       'apply': {'result': apply}, 'dispose': {'result': dispose}}
            ready = intake == observe == 'success'
            self.assertEqual(admitted(caller['apply'], results), ready and action == 'apply')
            self.assertEqual(admitted(caller['dispose'], results), ready and action == 'dispose')
            complete = (action == 'apply' and apply == 'success' and dispose == 'skipped' or
                        action == 'dispose' and dispose == 'success' and apply == 'skipped' or
                        action == 'inspect' and apply == dispose == 'skipped')
            self.assertEqual(admitted(caller['evidence'], results), ready and complete)

    def test_state_requires_successful_evidence_without_combining_job_credentials(self):
        children = jobs('evidence')
        self.assertEqual(set(children), {'evidence', 'state'})
        self.assertEqual(field(children['state'], 'needs'), '[evidence]')
        for result in ('success', 'failure', 'cancelled', 'skipped', 'timed_out'):
            self.assertEqual(admitted(children['state'], {'evidence': {'result': result}}), result == 'success')
        for role, body in children.items():
            other = 'STATE' if role == 'evidence' else 'EVIDENCE'
            self.assertNotIn('WHARF_LANE_' + other + '_ACCESS_KEY_ID:', body)
            self.assertNotIn('WHARF_LANE_' + other + '_SECRET_ACCESS_KEY:', body)
            self.assertNotIn('WHARF_LANE_DEPLOYMENT_TOKEN:', body)
            self.assertNotIn('WHARF_LANE_DISPOSAL_TOKEN:', body)
        self.assertIn('WHARF_REFERENCE: ${{ needs.evidence.outputs.reference }}', children['state'])


if __name__ == '__main__':
    unittest.main()
