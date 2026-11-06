"""Structured MCP tools retain ownership guards while human grammar changes."""
import pytest

from robot_control.mcp_server import create_server
from test_example_controller import example_rc


@pytest.mark.asyncio
async def test_native_control_and_read_queries_use_new_command_contract(example_rc):
    server = create_server(example_rc)
    for tool, args in [('policies', {}), ('show', {'scope': 'rc'}), ('show', {'scope': 'policy'}), ('show', {'scope': 'global'})]:
        reply = await server.call_tool(tool, args)
        assert not reply.is_error, reply
        if args.get('scope') == 'rc':
            assert reply.structured_content['schema_version'] == 3
    for old in ('take', 'return', 'show rc', 'policies', 'record on', 'switch example'):
        reply = await server.call_tool('command', {'line': old, 'wait': True})
        assert reply.is_error
    assert example_rc.control_owner == 'policy'
    taken = await server.call_tool('take', {'wait': True})
    assert not taken.is_error, taken
    assert example_rc.control_owner == 'mcp'
    returned = await server.call_tool('return', {'wait': True})
    assert not returned.is_error, returned
    assert example_rc.control_owner == 'policy'


@pytest.mark.asyncio
async def test_policy_config_show_without_take_and_set_preserves_raw_value(example_rc):
    server = create_server(example_rc)
    names = {tool.name for tool in await server.list_tools()}
    assert 'policy_config' in names
    shown = await server.call_tool('policy_config', {'instance_id': 'example', 'wait': True})
    assert not shown.is_error, shown
    assert shown.structured_content['result']['id'] == 'example'
    denied = await server.call_tool('policy_config', {
        'instance_id': 'example', 'action': 'set', 'key': 'params.message', 'value_text': '"未经接管"', 'wait': True})
    assert denied.is_error
    assert not (await server.call_tool('take', {'wait': True})).is_error
    worker, pid = example_rc._worker, example_rc._worker.pid
    changed = await server.call_tool('policy_config', {
        'instance_id': 'example', 'action': 'set', 'key': 'params.message',
        'value_text': '"原始 空格 [bold]"', 'wait': True})
    assert not changed.is_error, changed
    assert example_rc.query('policy example config show')['params']['message'] == '原始 空格 [bold]'
    assert example_rc._worker is worker and worker.pid == pid
    assert example_rc.control_owner == 'mcp' and example_rc.status()['state'] == 'paused'
    identity = changed.structured_content['operation_id']
    receipts = [row['message'] for row in example_rc.read_events(limit=1000)['events']
                if row['message'].startswith('MCP policy_config ') and identity in row['message']]
    assert any('queued:' in row for row in receipts)
    assert any('succeeded:' in row for row in receipts)


@pytest.mark.asyncio
async def test_native_record_controls_map_on_off_to_new_text_actions(example_rc):
    server = create_server(example_rc)
    owner, generation = example_rc.control_owner, example_rc.buffer.generation
    started = await server.call_tool('record', {'action': 'on', 'wait': True})
    assert not started.is_error, started
    stopped = await server.call_tool('record', {'action': 'off', 'wait': True})
    assert not stopped.is_error, stopped
    assert stopped.structured_content['result']['state'] == 'idle'
    assert example_rc.control_owner == owner and example_rc.buffer.generation == generation


@pytest.mark.asyncio
async def test_native_group_show_and_exact_instance_config_remain_distinct(example_rc):
    server = create_server(example_rc)
    assert not (await server.call_tool('take', {'wait': True})).is_error
    applied = await server.call_tool('policy_chain', {'policies': ['example', 'example'], 'wait': True})
    assert not applied.is_error, applied
    shown = await server.call_tool('show', {'scope': 'policy'})
    assert not shown.is_error, shown
    assert shown.structured_content['kind'] == 'chain'
    first = await server.call_tool('policy_config', {'instance_id': 'example-0', 'wait': True})
    assert not first.is_error, first
    assert first.structured_content['result']['id'] == 'example-0'
    ambiguous = await server.call_tool('policy_config', {'instance_id': 'example', 'wait': True})
    assert ambiguous.is_error
