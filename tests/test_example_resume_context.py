"""Optional context resume over real example workers, including native self chains."""
import json
import pytest

from config_helpers import read_config
from project_templates import copy_templates
from test_example_controller import ROOT, example_rc, command
from test_controller import until


@pytest.mark.parametrize('chain', [False, True, 'mixed'])
def test_example_resume_and_explicit_reset_are_distinct(example_rc, chain):
    controller = example_rc
    root = controller.root
    entry = root / 'policy/example/main.py'
    original = entry.read_text()
    callbacks = '''
_original_reset = reset_context
_original_example = example
def reset_context(context):
    _original_reset(context)
    context['test_resets'] = context.get('test_resets', 0) + 1
def resume_context(context):
    context['test_resumes'] = context.get('test_resumes', 0) + 1
def example(context):
    return {**_original_example(context), 'test_resets': context.get('test_resets', 0),
            'test_resumes': context.get('test_resumes', 0)}
'''
    entry.write_text(original + callbacks)
    if chain:
        copy_templates(ROOT / 'policy/example_chain', root / 'policy/example_chain')
    if chain == 'mixed':
        legacy = copy_templates(ROOT / 'policy/example', root / 'policy/legacy')
        manifest = read_config(legacy / 'policy.yaml')
        manifest['id'] = 'legacy'
        (legacy / 'policy.yaml').write_text(json.dumps(manifest))
        (legacy / 'main.py').write_text(original + callbacks.replace(
            "def resume_context(context):\n    context['test_resumes'] = context.get('test_resumes', 0) + 1\n", ''))
        (root / 'policy/example_chain/policy.yaml').write_text(json.dumps({
            'schema_version': 3, 'id': 'example_chain', 'kind': 'chain', 'policies': ['example', 'legacy']}))
    command(controller, 'policy switch ' + ('example_chain' if chain else 'example'))
    names = ['example-0', 'example-1'] if chain is True else ['example']
    for _ in range(2):
        command(controller, 'resume')
        until(lambda: controller.status()['consumed'] >= 1)
        command(controller, 'pause')
    for name in names:
        value = command(controller, f'policy {name} example')
        assert value['test_resets'] == 0 and value['test_resumes'] == 2
    if not chain:
        session = command(controller, 'record auto on')['session_id']
    command(controller, 'reset')
    assert controller.status()['state'] == 'paused'
    for name in names:
        value = command(controller, f'policy {name} example')
        assert value['test_resets'] == 1 and value['test_resumes'] == 2
        assert value['calls'] == 0 and value['observations'] == 0
    if chain == 'mixed':
        value = command(controller, 'policy legacy example')
        assert value['test_resets'] == 2 and value['test_resumes'] == 0
    if not chain:
        until(lambda: controller.status()['recording']['state'] == 'recording'
              and controller.status()['recording']['session_id'] != session
              and controller.status()['recording']['auto_pending'] == 0, 8)
        session = controller.status()['recording']['session_id']
        assert len(list((controller.outputs / 'logs').glob('record_*'))) == 2
        command(controller, 'resume')
        command(controller, 'pause')
        assert controller.status()['recording']['session_id'] == session
        assert controller.status()['recording']['auto_pending'] == 0
