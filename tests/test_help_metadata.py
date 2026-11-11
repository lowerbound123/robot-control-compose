"""Help declarations remain cold, bounded metadata, never execution rules."""
import copy

import pytest

from rc_policy_sdk.catalog import validate_policy
from rc_policy_sdk.commands import command_specs, validate_arguments
from rc_policy_sdk.argument_hints import normalize_config_hints


def manifest():
    return {'schema_version': 3, 'id': 'example', 'execution': {
        'mode': 'sync', 'control_mode': 'qpos', 'call_every_actions': 1},
        'params': {}, 'help': '中文 [literal]', 'tui_commands': {
            'choose': {'handler': 'choose', 'args': [{'name': 'mode', 'type': 'enum',
                'choices': ['safe'], 'help': '提示候选'}], 'help': '选择模式'}}}


DETAILS = {'group': '任务设置', 'description': '普通文本 [red] 不执行',
    'sections': [{'title': '执行影响', 'body': '保留执行进程。'}],
    'examples': [{'title': '设置消息', 'command': 'policy <实例> choose "你好"'}]}


def test_full_help_metadata_is_normalized_at_all_three_scopes_without_mutation():
    value = manifest()
    value['help_details'] = copy.deepcopy(DETAILS)
    value['tui_commands']['choose']['help_details'] = copy.deepcopy(DETAILS)
    value['config_hints'] = {'help': '配置', 'help_details': copy.deepcopy(DETAILS), 'settings': {}}
    original = copy.deepcopy(value)
    validate_policy(value)
    spec = command_specs(value)['choose']
    assert spec['help_details'] == DETAILS
    assert normalize_config_hints(value['config_hints'])['help_details'] == DETAILS
    assert value == original
    assert validate_arguments(['outside-the-hint'], spec) == ['outside-the-hint']


@pytest.mark.parametrize('details', [
    'text', None, {'callback': 'execute'}, {'description': 3}, {'group': []},
    {'description': 'x' * 16385}, {'description': '\x1b[31mcolor'},
    {'sections': {}}, {'sections': [{'title': 'ok', 'body': 3}]},
    {'sections': [{'title': 'ok', 'body': 'ok', 'style': 'red'}]},
    {'sections': [{'title': 'ok', 'body': 'ok'}] * 33},
    {'examples': [{'title': 'ok', 'command': 'a', 'eval': True}]},
    {'examples': [{'title': 'ok'}]}, {'examples': [{'title': 'ok', 'command': 'x'}] * 65},
])
@pytest.mark.parametrize('scope', ['policy', 'command', 'config'])
def test_invalid_help_metadata_rejected_during_cold_validation(details, scope):
    value = manifest()
    target = value if scope == 'policy' else value['tui_commands']['choose'] if scope == 'command' else value.setdefault('config_hints', {})
    target['help_details'] = details
    with pytest.raises(ValueError):
        validate_policy(value)


def test_simple_help_strings_remain_supported():
    value = manifest()
    validate_policy(value)
    assert command_specs(value)['choose']['help'] == '选择模式'


def test_configure_cannot_change_help_metadata():
    from rc_policy_sdk.configuration import validate_candidate
    value = manifest()
    value['help_details'] = copy.deepcopy(DETAILS)
    changed = copy.deepcopy(value)
    changed['help_details']['description'] = 'changed'
    with pytest.raises(ValueError, match='metadata'):
        validate_candidate(value, changed, None)


def test_chain_top_level_help_is_static_and_bounded():
    config = {'schema_version': 3, 'id': 'example_chain', 'kind': 'chain', 'policies': ['example', 'example'],
              'help': '原生链', 'help_details': copy.deepcopy(DETAILS)}
    validate_policy(config)
    config['help_details']['callback'] = 'never execute'
    with pytest.raises(ValueError):
        validate_policy(config)


@pytest.mark.parametrize('scope', ['parameter', 'config'])
def test_help_hints_cannot_inject_terminal_escape_sequences(scope):
    value = manifest()
    if scope == 'parameter':
        value['tui_commands']['choose']['args'][0]['help'] = '\x1b[31munsafe'
    else:
        value['config_hints'] = {'help': '\x1b[31munsafe'}
    with pytest.raises(ValueError):
        validate_policy(value)
