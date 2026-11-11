"""Public help uses static metadata, exact instances, and two consistent projections."""
import copy
import io
import json

import pytest

from config_helpers import read_config
from project_templates import copy_templates
from robot_control.help_document import help_text
from robot_control.terminal_commands import TerminalCommandRouter
from test_controller import setup_controller, ready, until
from test_example_controller import ROOT, command, example_rc, example_mcp
from test_mcp import client


def test_unloaded_source_and_chain_help_never_import_or_start_policy(setup_controller):
    rc, _, root = setup_controller
    rc.config['startup_policy'] = None
    rc.start()
    until(lambda: rc._connected)
    directory = root / 'policy/second'
    config = read_config(directory / 'policy.yaml')
    config.update(help='未加载来源', help_details={'description': '原样 ${HOME} {value} [bold]',
        'examples': [{'title': '占位符', 'command': 'policy <实例> say "你好"'}]},
        tui_commands={'say': {'handler': 'say', 'args': ['str'], 'help': '声明命令'}},
        params={**config['params'], 'api_key': 'NEVER-READ-LIVE-SECRET', 'private': 'LIVE-CONFIG-NOT-HELP'})
    (directory / 'policy.yaml').write_text(json.dumps(config))
    (directory / 'main.py').write_text('raise RuntimeError("help imported a model")')
    chain = root / 'policy/demo_chain'
    chain.mkdir()
    (chain / 'policy.yaml').write_text(json.dumps({'schema_version': 3, 'kind': 'chain',
        'policies': ['second', 'second'], 'help': '静态原生链',
        'help_details': {'sections': [{'title': '顺序', 'body': '两个独立成员'}],
            'examples': [{'title': '查询', 'command': 'policy help demo_chain'}]}}))
    for line in ('policy help second', 'policy help demo_chain'):
        payload = rc.query(line)
        text = payload['help']
        assert text == help_text(payload['help_document'])
        assert '<实例>' in text and '未加载' in text and '${HOME} {value} [bold]' in text
        assert 'NEVER-READ-LIVE-SECRET' not in text and 'LIVE-CONFIG-NOT-HELP' not in text
        assert rc._worker is None
    payload = rc.query('policy help demo_chain')
    assert '静态原生链' in payload['help'] and 'second → second' in payload['help']
    assert len(payload['policies']) == 2 and payload['chains'][0]['source_id'] == 'demo_chain'
    assert [row['planned_instance_id'] for row in payload['policies']] == ['second-0', 'second-1']


def test_example_repeated_instances_bind_help_independently_without_lifecycle_work(example_rc):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    workers = rc._worker.member_workers()
    before = rc.status()
    all_help = rc.query('policy help example')
    assert [row['instance_id'] for row in all_help['policies']] == ['example-0', 'example-1']
    assert 'policy example-0 set-message' in all_help['help']
    assert 'policy example-1 set-message' in all_help['help']
    detail = rc.query('policy help example-1 set-message')
    assert 'policy example-0' not in detail['help']
    assert 'policy example-1 set-message "整理桌面"' in detail['help']
    assert '<实例>' not in detail['help']
    assert {p['name'] for p in detail['help_document']['parameters']} == {'message'}
    config_help = rc.query('policy help example-1 config')
    assert 'policy example-1 config set params.message' in config_help['help']
    assert '配置设置' in config_help['help'] and '示例与验证' in config_help['help']
    assert not config_help['policies'][0]['commands']
    assert any('params.message' in row['name'] for row in config_help['help_document']['parameters'])
    assert rc._worker.member_workers() == workers
    for key in ('generation', 'control_epoch', 'control_owner', 'state'):
        assert rc.status()[key] == before[key]


@pytest.mark.asyncio
async def test_http_help_command_and_batch_keep_distinct_receipt_shapes(example_rc, example_mcp):
    rc = example_rc
    before = rc.status()
    async with client(example_mcp.url) as session:
        for args in ({}, {'policy_id': 'example', 'command': 'set-message'},
                     {'tool': 'send_command'}, {'topic': 'config rc set'}, {'tool': 'move_eef'}):
            reply = await session.call_tool('help', args)
            assert not reply.is_error, reply
            assert reply.content[0].text == help_text(reply.structured_content['help_document'])
            assert not reply.content[0].text.startswith('{')
        tool = await session.call_tool('help', {'tool': 'move_eef'})
        rows = {row['name']: row for row in tool.structured_content['help_document']['parameters']}
        assert rows['targets.<key>.end_pos']['required'] is True
        assert 'default' not in rows['targets.<key>.end_pos']
        assert rows['expected_control.run_id']['required'] is True
        assert (await session.call_tool('help', {'tool': 'status', 'topic': 'status'})).is_error
        for line in ('help', 'help config rc set', 'policy help example set-message'):
            reply = await session.call_tool('command', {'line': line})
            receipt = reply.structured_content
            assert receipt['status'] == 'succeeded' and receipt['operation_id']
            assert reply.content[0].text == help_text(receipt['result']['help_document'])
            operation = await session.call_tool('get_operation', {'operation_id': receipt['operation_id']})
            assert json.loads(operation.content[0].text) == operation.structured_content
        batch = await session.call_tool('tui_command', {'text': 'help config rc set\nstatus', 'wait': True})
        receipt = batch.structured_content
        assert receipt['result']['kind'] == 'tui_batch'
        assert receipt['result']['steps'][0]['result']['help_document']['schema_version'] == 1
        assert json.loads(batch.content[0].text) == receipt
        status = await session.call_tool('status', {})
        assert json.loads(status.content[0].text) == status.structured_content
    for key in ('generation', 'control_epoch', 'control_owner', 'state'):
        assert rc.status()[key] == before[key]


def test_terminal_and_headless_help_emit_full_sanitized_plain_projection(setup_controller):
    from robot_control.cli import drain_events
    from robot_control.help_document import document
    rc, _, _ = setup_controller
    ready(rc)
    cursor = rc.read_events()['next_seq']
    receipt = rc.submit('help config rc set')
    assert rc.get_operation(receipt)['status'] == 'succeeded'
    value = document('测试 [literal]', '原样 ${HOME}', sections=[{'title': 'JSON', 'body': '{"api_key":"NO-LEAK","safe":1}'}])
    original = copy.deepcopy(value)
    rc._emit_command('ignored', help_document=value)
    events = [row for row in rc.read_events(cursor)['events'] if 'help_document' in row]
    assert len(events) == 2
    for event in events:
        assert event['message'] == help_text(event['help_document'])
        assert '\x1b' not in event['message'] and 'NO-LEAK' not in event['message']
    assert value == original
    stream = io.StringIO()
    while drain_events(rc, stream):
        pass
    assert 'config rc set frequency_hz 20' in stream.getvalue()
    assert '[literal]' in stream.getvalue() and 'NO-LEAK' not in stream.getvalue()


@pytest.mark.parametrize('scope', ['all', 'key'])
@pytest.mark.parametrize('complete_document', [False, True])
def test_controller_global_json_help_keys_remain_ordinary_data(setup_controller, scope, complete_document):
    from robot_control.help_document import document
    rc, _, _ = setup_controller
    ready(rc)
    payload = {'help_document': {'user': 'ordinary data'}, 'unrelated': ['keep', 7]}
    if complete_document:
        value = document('普通 JSON 字段', '这些键不是帮助指令')
        payload.update(help_document=value, help=help_text(value))
    rc.globals.patch(payload if scope == 'all' else {'sample': payload})
    before = rc.status()
    cursor = rc.read_events()['next_seq']
    identity = rc.submit('global show' if scope == 'all' else 'global show sample')
    receipt = rc.get_operation(identity)
    assert receipt['status'] == 'succeeded' and receipt['result'] == payload
    events = rc.read_events(cursor)['events']
    assert events and all(not event['message'].startswith('命令失败') for event in events)
    assert all('help_document' not in event for event in events)
    assert json.loads(events[-1]['message']) == payload
    for key in ('generation', 'control_epoch', 'control_owner', 'state'):
        assert rc.status()[key] == before[key]


def test_terminal_builtin_policy_and_mcp_help_paths_are_readonly(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    before = rc.status()
    router = TerminalCommandRouter(rc)
    try:
        for path in ('policy switch', 'policy list', 'policy manage apply', 'mcp take'):
            cursor = rc.read_events()['next_seq']
            identity = router.submit('help ' + path)
            receipt = rc.get_operation(identity)
            assert receipt['status'] == 'succeeded'
            payload = receipt['result']
            assert payload['help_document']['title'] == path
            assert payload['help'] == help_text(payload['help_document'])
            assert rc.read_events(cursor)['events'][-1]['message'] == payload['help']
        for key in ('generation', 'control_epoch', 'control_owner', 'state'):
            assert rc.status()[key] == before[key]
    finally:
        router.close()


@pytest.mark.asyncio
async def test_http_help_topic_reaches_builtin_policy_and_mcp_paths(example_rc, example_mcp):
    before = example_rc.status()
    async with client(example_mcp.url) as session:
        for topic in ('policy switch', 'policy list', 'policy manage apply', 'mcp take'):
            reply = await session.call_tool('help', {'topic': topic})
            assert not reply.is_error, reply
            payload = reply.structured_content
            assert payload['help_document']['title'] == topic
            assert reply.content[0].text == payload['help'] == help_text(payload['help_document'])
        for topic in ('policy swit', 'mcp ta'):
            assert (await session.call_tool('help', {'topic': topic})).is_error
    for key in ('generation', 'control_epoch', 'control_owner', 'state'):
        assert example_rc.status()[key] == before[key]


def test_terminal_projects_mcp_command_help_without_dumping_receipt(example_rc, example_mcp):
    rc = example_rc
    router = TerminalCommandRouter(rc, example_mcp)
    cursor = rc.read_events()['next_seq']
    try:
        response = router.submit('mcp command {"line":"policy help example set-message"}').result(5)
        until(lambda: not router.pending)
        assert response.structured_content['operation_id']
        events = rc.read_events(cursor)['events']
        help_events = [event for event in events if 'help_document' in event]
        assert help_events and all(event['message'] == help_text(event['help_document']) for event in help_events)
        assert all('"operation_id"' not in event['message'] for event in help_events)
        assert 'policy example set-message "整理桌面"' in help_events[-1]['message']
    finally:
        router.close()


def test_cli_help_is_chinese_and_performs_no_startup(monkeypatch, capsys):
    from robot_control import cli
    def forbidden(*args, **kwargs):
        raise AssertionError('help attempted startup')
    monkeypatch.setattr(cli, 'make_controller', forbidden)
    monkeypatch.setattr(cli, 'make_mcp_service', forbidden)
    with pytest.raises(SystemExit) as exit:
        cli.main(['--help'])
    assert exit.value.code == 0
    text = capsys.readouterr().out
    assert '启动与配置' in text and '终端与服务' in text and '--headless' in text
