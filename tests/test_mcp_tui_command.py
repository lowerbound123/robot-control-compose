"""HTTP and local MCP boundary for human text batches (no business policies)."""
import asyncio

import pytest

from test_controller import setup_controller, ready
from test_mcp import client


@pytest.fixture
def batch_service(setup_controller):
    from robot_control.mcp_server import MCPService
    rc, _, _ = setup_controller
    ready(rc)
    service = MCPService(rc, {'port': 0}, allow_ephemeral=True)
    service.start()
    try:
        yield rc, service
    finally:
        service.close()


def test_http_batches_need_no_take_and_survive_client_disconnect(batch_service):
    rc, service = batch_service
    async def issue():
        async with client(service.url) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert 'tui_command' in tools, 'human batch tool is missing'
            assert 'Agents should not use' in tools['tui_command'].description
            denied = await session.call_tool('command', {'line': 'reset'})
            assert denied.is_error
            reply = await session.call_tool('tui_command', {'text': 'policy first config set params.value 7\npolicy first config show'})
            assert not reply.is_error, reply
            return reply.structured_content['operation_id']
    identity = asyncio.run(issue())
    reply = rc.wait_operation(identity, 5)
    assert reply['status'] == 'succeeded', reply
    assert reply['result']['steps'][1]['result']['params']['value'] == 7
    assert rc.control_owner == 'policy'


def test_local_wait_timeout_cancel_and_sensitive_preflight(batch_service):
    rc, service = batch_service
    tool_names = {row['name'] for row in service.tools}
    assert {'tui_command', 'cancel_tui_command'} <= tool_names
    reply = service.submit_tool('tui_command', {'text': 'status\nstatus', 'wait': True, 'timeout_s': 0}).result(3)
    identity = reply.structured_content['operation_id']
    assert not reply.is_error
    assert rc.wait_operation(identity, 3)['status'] == 'succeeded'
    reply = service.submit_tool('cancel_tui_command', {'operation_id': identity}).result(3)
    assert reply.structured_content['status'] == 'succeeded'
    for text in ('status\nmcp take', 'status\nglobal set api_key "batch-secret-canary"'):
        denied = service.submit_tool('tui_command', {'text': text}).result(3)
        assert denied.is_error and 'operation_id' not in denied.structured_content
    assert 'batch-secret-canary' not in str(rc.read_events(limit=1000))
