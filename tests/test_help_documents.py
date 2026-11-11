import json

import pytest


def test_builtin_help_is_grouped_and_complete_token_paths_are_required():
    from robot_control.help_content import builtin_help
    from robot_control.help_document import help_text, help_lines
    from rc_policy_sdk.command_syntax import parse_command
    overview = builtin_help()
    assert {'控制与状态', '机器人连接', '策略与配置'} <= {row['group'] for row in overview['commands']}
    detail = builtin_help(('config', 'rc', 'set'))
    text = help_text(detail)
    assert '重启' in text and '草稿' in text and 'config rc save' in text
    assert 'config rc set frequency_hz 20' in text
    assert '\n'.join(line.text for line in help_lines(detail)) == text
    assert json.loads(json.dumps(detail, ensure_ascii=False)) == detail
    assert parse_command('help config rc set').args == ('config', 'rc', 'set')
    with pytest.raises(ValueError):
        parse_command('help config rc s')


@pytest.mark.parametrize('path', ['policy switch', 'policy list', 'policy manage apply', 'mcp take', 'mcp return'])
def test_builtin_policy_and_mcp_help_paths_are_reachable(path):
    from rc_policy_sdk.command_syntax import parse_command
    from robot_control.help_content import builtin_help
    parsed = parse_command('help ' + path)
    assert parsed.kind == 'help' and parsed.args == tuple(path.split())
    value = builtin_help(parsed.args)
    assert value['title'] == path and value['usage'] and value['examples']


@pytest.mark.parametrize('path', ['policy swit', 'policy manage app', 'mcp ta', 'mcp ret'])
def test_builtin_help_rejects_truncated_tokens(path):
    from rc_policy_sdk.command_syntax import parse_command
    with pytest.raises(ValueError, match='未知帮助主题'):
        parse_command('help ' + path)


@pytest.mark.parametrize('scope', ['policy', 'mcp'])
def test_bare_namespace_help_keeps_canonical_discovery_guidance(scope):
    from rc_policy_sdk.command_syntax import parse_command
    with pytest.raises(ValueError, match='请使用 ' + scope + ' help'):
        parse_command('help ' + scope)


def test_policy_switch_help_declares_reset_once_as_an_optional_flag():
    from robot_control.help_content import builtin_help
    parameters = builtin_help('policy switch')['parameters']
    assert [row['name'] for row in parameters] == ['id', '--reset']
    reset = parameters[1]
    assert reset['type'] == 'flag' and reset['required'] is False
    assert '复位' in reset['description']


def test_schema_help_expands_references_unions_enums_and_nested_required():
    from robot_control.mcp_help import schema_parameters
    schema = {'$defs': {'Target': {'type': 'object', 'properties': {
        'mode': {'enum': ['left', 'right'], 'type': 'string'},
        'speed': {'type': 'number', 'default': .2}}, 'required': ['mode']}},
        'type': 'object', 'properties': {
            'target': {'anyOf': [{'$ref': '#/$defs/Target'}, {'type': 'null'}], 'default': None},
            'steps': {'type': 'array', 'items': {'$ref': '#/$defs/Target'}}},
        'required': ['steps']}
    rows = {row['name']: row for row in schema_parameters(schema)}
    assert rows['steps']['required'] is True
    assert rows['target']['type'] == 'object | null'
    assert rows['target']['default'] is None
    assert rows['target.mode']['choices'] == ['left', 'right']
    assert rows['target.mode']['required'] is True
    assert 'default' not in rows['target.mode']
    assert rows['steps[].speed']['default'] == .2


def test_send_command_help_explains_real_grammar_and_keeps_literal_brackets():
    from robot_control.mcp_help import mcp_help_document
    from robot_control.help_document import help_text
    tools = [{'name': 'send_command', 'input_schema': {'properties': {'commands': {'type': 'string'}}, 'required': ['commands']}}]
    text = help_text(mcp_help_document(tools, 'send_command'))
    for literal in ('[&motion]', '串行', '并行', '米', '弧度', 'clear_queue', 'home', 'reset_qpos', '不取消', 'props'):
        assert literal in text
    for motion in ('twist-clockwise', 'turn-right-across-table', 'hold', 'open', 'close'):
        assert motion in text


def test_regular_results_do_not_reinterpret_user_data_as_help():
    from robot_control.mcp_server import result
    payload = {'help_document': {'user': 'ordinary value'}, 'help': 'ordinary data'}
    reply = result(payload)
    assert json.loads(reply.content[0].text) == payload


def test_schema_array_references_and_recursive_objects_remain_bounded():
    from robot_control.mcp_help import schema_parameters
    schema = {'$defs': {
        'Mode': {'type': 'string', 'enum': ['a', 'b']},
        'Node': {'type': 'object', 'properties': {'child': {'$ref': '#/$defs/Node'}}}},
        'properties': {'modes': {'type': 'array', 'items': {'$ref': '#/$defs/Mode'}},
                       'node': {'$ref': '#/$defs/Node'}}}
    rows = schema_parameters(schema)
    assert rows[0]['type'] == 'array<string>'
    assert len(rows) < 10
