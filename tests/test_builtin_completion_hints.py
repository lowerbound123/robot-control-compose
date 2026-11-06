"""Shipped manifests expose useful hints through the public capability model."""
from pathlib import Path

import pytest

from config_helpers import read_config
from robot_control.policy_capabilities import PolicyCapabilities
from rc_policy_sdk.commands import command_specs, parse_command

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('example', 'hold', 'llm_direction', 'llm_failure', 'pi05',
           'qwen35_single', 'qwen35_twostage', 'qwen35_history', 'internvla_maas')


@pytest.mark.parametrize('source', SOURCES)
def test_builtin_settings_are_discoverable_without_loading_policy(source):
    config = read_config(ROOT / 'policy' / source / 'policy.yaml.example')
    info = PolicyCapabilities._describe_commands(source, None, config)
    hints = info['config_hints']['settings']
    assert all('params.' + name in hints for name in config['params'])
    assert hints['execution.on_backend_switch']['choices'] == ['reset', 'reinitialize_policy']
    assert hints['execution.mode']['type'] == 'enum'
    assert config['execution']['mode'] in hints['execution.mode']['choices']
    assert hints['execution.control_mode']['type'] == 'enum'
    assert all(value.get('help') for value in hints.values())


@pytest.mark.parametrize('source,command,suffix', [
    ('pi05', 'switch-model', '.json'),
    ('internvla_maas', 'switch-model', '.json'),
    ('qwen35_single', 'switch-model', '.json'),
    ('qwen35_twostage', 'switch-model', '.json'),
    ('qwen35_history', 'switch-model', '.json'),
    ('llm_direction', 'switch-provider', '.yaml'),
    ('llm_failure', 'switch-provider', '.yaml'),
])
def test_file_arguments_expose_root_but_keep_string_execution(source, command, suffix):
    config = read_config(ROOT / 'policy' / source / 'policy.yaml.example')
    info = PolicyCapabilities._describe_commands(source, None, config)
    spec = next(item for item in info['commands'] if item['name'] == command)
    assert spec['args'] == ['str']
    assert spec['parameters'][0]['type'] == 'path'
    assert spec['parameters'][0]['root'] == '.'
    assert spec['parameters'][0]['kind'] == 'file'
    assert suffix in spec['parameters'][0]['suffixes']
    name, args = parse_command(command + ' "config folder/model' + suffix + '"', command_specs(config))
    assert name == command and args == ['config folder/model' + suffix]
