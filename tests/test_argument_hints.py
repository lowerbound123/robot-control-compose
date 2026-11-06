"""Cold hint metadata enriches discovery without narrowing executable arguments."""
import copy
import pytest

from rc_policy_sdk.commands import command_specs, command_usage, parse_command, validate_arguments
from rc_policy_sdk.catalog import validate_policy


def config(args):
    return {'schema_version': 3, 'id': 'example', 'execution': {'mode': 'sync', 'control_mode': 'qpos', 'call_every_actions': 1},
            'params': {}, 'tui_commands': {'choose': {'handler': 'choose', 'args': args}}}


def test_legacy_arguments_keep_execution_types_and_usage():
    spec = command_specs(config(['str', 'int', 'float', 'bool']))['choose']
    assert spec['args'] == ['str', 'int', 'float', 'bool']
    assert command_usage('choose', spec) == 'choose <str> <int> <float> <bool>'
    assert [p['type'] for p in spec['parameters']] == ['str', 'int', 'float', 'bool']
    assert parse_command('choose "two words" 3 .5 true', {'choose': spec}) == ('choose', ['two words', 3, .5, True])


def test_named_advisory_types_do_not_restrict_runtime_values():
    specs = command_specs(config([
        {'name': 'mode', 'type': 'enum', 'help': 'Select a mode', 'choices': ['safe', 'fast']},
        {'name': 'weights', 'type': 'path', 'root': 'weights', 'kind': 'file', 'suffixes': ['.pt']},
        {'name': 'payload', 'type': 'json'},
    ]))
    spec = specs['choose']
    assert spec['args'] == ['str', 'str', 'str']
    assert command_usage('choose', spec) == 'choose <mode> <weights> <payload>'
    assert spec['parameters'][0]['choices'] == ['safe', 'fast']
    assert validate_arguments(['experimental', '../outside.bin', 'raw invalid json'], spec) == ['experimental', '../outside.bin', 'raw invalid json']
    assert parse_command('choose experimental ../outside.bin raw', specs)[1] == ['experimental', '../outside.bin', 'raw']


def test_config_hints_normalize_opaque_keys_without_mutating_manifest():
    from rc_policy_sdk.argument_hints import normalize_config_hints
    hints = {'help': 'Runtime choices', 'settings': {'camera-selection': {'type': 'enum', 'choices': ['left', 'right']}, 'model/file': {'type': 'path', 'root': '/models'}}}
    before = copy.deepcopy(hints)
    result = normalize_config_hints(hints)
    assert result['settings']['camera-selection']['name'] == 'camera-selection'
    assert result['settings']['model/file']['root'] == '/models'
    assert hints == before
    candidate = config([])
    candidate['config_hints'] = hints
    validate_policy(candidate)


@pytest.mark.parametrize('parameter', [
    {'type': 'unknown'}, {'type': 'enum', 'choices': []}, {'type': 'enum', 'choices': [1]},
    {'type': 'str', 'name': ''}, {'type': 'path', 'root': 3}, {'type': 'path', 'kind': 'device'},
    {'type': 'path', 'suffixes': ['pt']}, {'type': 'int', 'root': '/models'}, {'type': 'str', 'unknown': True},
])
def test_bad_metadata_fails_cold_validation(parameter):
    with pytest.raises(ValueError): command_specs(config([parameter]))


def test_config_hint_validation_is_part_of_catalog():
    candidate = config([])
    candidate['config_hints'] = {'settings': {'bad': {'type': 'path', 'root': 8}}}
    with pytest.raises(ValueError): validate_policy(candidate)


def test_capabilities_expose_parameter_and_setting_hints_without_model_imports():
    from robot_control.policy_capabilities import PolicyCapabilities
    candidate = config([{'name': 'count', 'type': 'int', 'help': 'Number of rows'}])
    candidate['config_hints'] = {'help': 'Change settings', 'settings': {'target': {'type': 'str', 'help': 'Opaque target'}}}
    row = PolicyCapabilities._describe_commands('example', 'example-1', candidate)
    assert row['commands'][0]['args'] == ['int']
    assert row['commands'][0]['parameters'][0]['name'] == 'count'
    assert row['config_hints']['settings']['target']['help'] == 'Opaque target'


@pytest.mark.parametrize('key', ['camera selection', 'camera\nselection', 'camera\x00selection'])
def test_config_hint_keys_match_executable_key_contract(key):
    from rc_policy_sdk.argument_hints import normalize_config_hints
    with pytest.raises(ValueError):
        normalize_config_hints({'settings': {key: {'type': 'str'}}})
