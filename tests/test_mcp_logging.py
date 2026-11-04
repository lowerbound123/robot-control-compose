"""Remote manual control remains observable after the MCP caller stops waiting."""
import asyncio
import io
import json

import pytest

from robot_control.cli import drain_events
from robot_control.direct_commands import parse_commands
from robot_control.log_format import event_metadata
from test_controller import setup_controller, until
from test_mcp import client
from test_mcp_local import local_call, local_service


def receipts(controller, tool, identity):
    return [row for row in controller.read_events(limit=1000)['events']
            if row['message'].startswith(f'MCP {tool} ') and identity in row['message']]


@pytest.mark.parametrize('level', ['INFO', 'CRITICAL'])
@pytest.mark.parametrize('tool, arguments', [
    ('send_command', {'commands': ['left:up:0.01@fast', 'left:up:0.01@fast']}),
    ('move_eef', {'targets': {'left': {'end_pos': [.01, 0, 0, 0, 0, 0]}}}),
    ('move_joints', {'targets': {'right': {'joint_pos': [.01] * 6}}}),
])
def test_http_motion_receipts_survive_disconnect_and_reach_all_log_sinks(local_service, tool, arguments, level):
    controller, _, service = local_service
    controller.config['logging']['level'] = level

    async def issue():
        async with client(service.url) as session:
            taken = await session.call_tool('take', {})
            assert not taken.is_error
            reply = await session.call_tool(tool, {**arguments, 'wait': False})
            assert not reply.is_error
            return taken.structured_content['operation_id'], reply.structured_content['operation_id']

    take_id, identity = asyncio.run(issue())
    assert controller.wait_operation(identity, 3)['status'] == 'succeeded'
    until(lambda: len(receipts(controller, tool, identity)) == 2)
    rows = receipts(controller, tool, identity)
    assert ' queued:' in rows[0]['message'] and ' succeeded:' in rows[1]['message']
    assert all(row['level'] == 'info' and event_metadata(row)[2] == 'MCP' for row in rows)
    assert json.loads(rows[1]['message'].split(': ', 1)[1])['result']['completed_steps'] == (2 if tool == 'send_command' else 1)
    assert len(receipts(controller, 'take', take_id)) == 2
    assert not any(row['level'] == 'trace' for row in controller.read_events(limit=1000)['events'])
    output = io.StringIO()
    drain_events(controller, output)
    assert all(row['message'] in output.getvalue() for row in rows)
    logfile = controller.outputs / 'logs' / f'rc_{controller.run_id}.jsonl'
    until(lambda: all(row['message'] in [json.loads(line)['message'] for line in logfile.read_text().splitlines()]
                      for row in rows))
    # Receipt polling must not replay the terminal log or create events-of-events.
    before = controller.read_events(limit=1000)
    for _ in range(2):
        local_call(service, 'get_operation', {'operation_id': identity})
        local_call(service, 'state')
        local_call(service, 'events')
    assert controller.read_events(limit=1000) == before


def test_return_logs_cancellation_once_after_wait_timeout(local_service):
    controller, _, service = local_service
    local_call(service, 'take')
    reply = local_call(service, 'send_command', {'commands': 'left:up:1@slow', 'wait': True, 'timeout_s': 0})
    identity = reply.structured_content['operation_id']
    assert reply.structured_content['status'] in ('queued', 'running')
    until(lambda: controller.direct.active is not None)
    assert not any(' succeeded:' in row['message'] or ' cancelled:' in row['message']
                   for row in receipts(controller, 'send_command', identity))
    returned = local_call(service, 'return', {'wait': True})
    assert returned.structured_content['status'] == 'succeeded'
    until(lambda: len(receipts(controller, 'return', returned.structured_content['operation_id'])) == 2)
    rows = receipts(controller, 'send_command', identity)
    assert len(rows) == 2
    assert rows[-1]['level'] == 'warning' and ' cancelled:' in rows[-1]['message']
    assert 'control returned to policy' in rows[-1]['message']


def test_failed_motion_logs_actual_error_and_step_counts(local_service):
    controller, robot, service = local_service
    local_call(service, 'take')
    controller.direct.config['step_timeout_s'] = .06
    robot.feedback_since = lambda arm, seq: []
    reply = local_call(service, 'send_command', {'commands': 'left:close', 'wait': True})
    identity = reply.structured_content['operation_id']
    assert reply.is_error and reply.structured_content['status'] == 'failed'
    until(lambda: len(receipts(controller, 'send_command', identity)) == 2)
    row = receipts(controller, 'send_command', identity)[-1]
    assert row['level'] == 'error' and ' failed:' in row['message']
    assert 'timeout waiting for fresh arrival feedback' in row['message']
    assert json.loads(row['message'].split(': ', 1)[1])['result']['failed_steps'] == 1


def test_pause_before_dispatch_still_logs_cancelled_motion(local_service):
    controller, _, service = local_service
    local_call(service, 'take')
    with controller._lock:
        identity = controller.submit_direct(parse_commands('left:up:0.01'))
        controller.submit('pause')
    assert controller.wait_operation(identity, 2)['status'] == 'cancelled'
    until(lambda: len(receipts(controller, 'send_command', identity)) == 2)
    assert 'cancelled by pause' in receipts(controller, 'send_command', identity)[-1]['message']


def test_trace_contains_resolved_targets_without_repeated_completion_logs(local_service):
    controller, _, service = local_service
    controller.config['logging']['level'] = 'TRACE'
    local_call(service, 'take')
    reply = local_call(service, 'send_command', {'commands': ['left:up:0.01@fast', 'left:up:0.02@fast'], 'wait': True})
    identity = reply.structured_content['operation_id']
    until(lambda: len(receipts(controller, 'send_command', identity)) == 2)
    rows = [row for row in controller.read_events(limit=1000)['events']
            if row['message'].startswith('MCP motion step:') and identity in row['message']]
    assert len(rows) == 2 and all(row['level'] == 'trace' for row in rows)
    targets = [json.loads(row['message'].split(': ', 1)[1]) for row in rows]
    assert [row['step'] for row in targets] == [1, 2]
    assert [row['targets']['left']['values'][2] for row in targets] == pytest.approx([.01, .03])
    assert targets[1]['command'] == 'left:up:0.02@fast'


def test_rejected_requests_are_logged_without_echoing_secrets(local_service):
    controller, _, service = local_service
    denied = local_call(service, 'send_command', {'commands': 'left:up:0.01'})
    assert denied.is_error
    assert any('MCP send_command rejected:' in row['message'] and 'read-only' in row['message']
               for row in controller.read_events()['events'])
    local_call(service, 'take')
    invalid = local_call(service, 'send_command', {'commands': 'left:up:-1'})
    assert invalid.is_error
    secret = local_call(service, 'command', {'line': 'set global.api_key "do-not-log-this-secret"'})
    assert secret.is_error
    messages = [row['message'] for row in controller.read_events()['events']]
    assert any('MCP command rejected:' in message for message in messages)
    assert all('do-not-log-this-secret' not in message for message in messages)
