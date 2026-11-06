"""Human command grammar: explicit targets, no legacy execution aliases."""
import pytest

from rc_policy_sdk.command_syntax import parse_command, POLICY_COMMAND_RESERVED


@pytest.mark.parametrize(('text', 'kind', 'target', 'value'), [
    ('status', 'status', None, None),
    ('config rc show', 'rc_config_show', None, None),
    ('config rc set frequency_hz 20', 'rc_config_set', 'frequency_hz', '20'),
    ('global set task {"label": "中文 空格"}', 'global_set', 'task', '{"label": "中文 空格"}'),
    ('global remove task', 'global_remove', 'task', None),
    ('policy example-1 config set params.message "你好  [bold]"', 'policy_config_set', 'example-1', '"你好  [bold]"'),
    ('policy example-1 example', 'policy_command', 'example-1', None),
    ('policy example-1 retry', 'policy_retry', 'example-1', None),
    ('policy switch example --reset', 'policy_switch', 'example', None),
    ('policy manage save', 'policy_manage', None, None),
    ('monitor', 'monitor', None, None),
    ('monitor example-1', 'monitor', 'example-1', None),
    ('snapshot', 'snapshot', None, None),
    ('record', 'record', None, None),
    ('record start', 'record', None, None),
    ('mcp take', 'mcp_tool', 'take', None),
    ('mcp help state', 'mcp_help', 'state', None),
])
def test_command_preserves_meaning_and_raw_values(text, kind, target, value):
    parsed = parse_command(text)
    assert (parsed.kind, parsed.target, parsed.value) == (kind, target, value)


@pytest.mark.parametrize('text', [
    'r', 'quit', 'take', 'return', 'switch example', 'policies', 'show rc',
    'set example.params.message "old"', 'save-config rc', 'record toggle',
    'record on', 'record off', 'snapshot save', 'monitor on', 'monitor-example on',
    'policy example command hello', 'mcp show rc', 'mcp', 'help mcp', 'manage-policy',
])
def test_old_syntax_is_rejected_instead_of_executed(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_custom_commands_cannot_shadow_subcommands():
    from rc_policy_sdk.commands import command_specs
    for name in ('save', 'close', 'apply', 'config', 'show', 'set', 'retry'):
        assert name in POLICY_COMMAND_RESERVED
        with pytest.raises(ValueError, match='reserved'):
            command_specs({'tui_commands': {name: {'handler': name, 'args': []}}})
