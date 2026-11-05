"""Web-discovered writes keep admission and dispatch generation checks."""
import pytest

from test_controller import setup_controller, ready
from test_example_controller import example_rc, example_mcp, command
from robot_control.mcp_server import MCPService


def guard(rc):
    value = rc.status()
    return {key: value[key] for key in ('run_id', 'control_epoch', 'generation')}


@pytest.mark.parametrize('name,args', [
    ('policy_command', {'instance_id': 'first', 'command': 'missing', 'args': []}),
    ('policy_chain', {'policies': ['first'], 'action': 'apply'}),
    ('retry_reload', {'instance_id': 'first'}),
    ('return_and_wait', {'timeout_s': 0}),
    ('send_command', {'commands': 'left:up:0.001@slow'}),
    ('move_eef', {'targets': {'left': {'end_pos': [0] * 6}}}),
    ('move_joints', {'targets': {'left': {'joint_pos': [0] * 6}}}),
])
def test_new_web_tools_reject_stale_guard_before_any_acceptance(setup_controller, name, args):
    rc, _, _ = setup_controller
    ready(rc)
    service = MCPService(rc, {'port': 0}, allow_ephemeral=True)
    service.start()
    try:
        stale = guard(rc)
        rc.wait_operation(rc.submit('take'), 3)
        response = service.submit_tool(name, {**args, 'expected_control': stale}).result(3)
        assert response.is_error
        assert 'control state changed' in str(response.structured_content or response.content)
        assert 'operation_id' not in (response.structured_content or {})
        assert rc.control_owner == 'mcp' and not rc.status()['returning']
    finally:
        service.close()


@pytest.mark.parametrize('kind', ['command', 'chain', 'motion', 'episode'])
def test_queued_web_writes_recheck_guard_before_dispatch(example_rc, kind):
    rc = example_rc
    command(rc, 'take')
    with rc._lock:
        expected = guard(rc)
        if kind == 'command':
            identity = rc.submit_policy_command('example', 'set-message', ['stale'], expected_control=expected)
        elif kind == 'chain':
            identity = rc.submit_policy_chain(['example', 'example'], expected_control=expected)
        elif kind == 'motion':
            from robot_control.direct_commands import parse_commands
            identity = rc.submit_direct(parse_commands('left:up:0.001@slow', 'slow'), expected_control=expected)
        else:
            identity = rc.submit_policy_wait(expected_control=expected)
        rc.buffer.invalidate()
    assert rc.wait_operation(identity, 5)['status'] in ('failed', 'cancelled')
    assert rc.control_owner == 'mcp'
    if kind == 'episode':
        assert rc.get_policy_result(identity)['finished']
        assert not rc.status()['returning'] and not rc.status()['policy_wait_operation_id']


def test_guarded_policy_command_accepts_native_typed_arguments(example_rc, example_mcp):
    rc = example_rc
    command(rc, 'take')
    response = example_mcp.submit_tool('policy_command', {
        'instance_id': 'example', 'command': 'set-message', 'args': ['guarded'],
        'expected_control': guard(rc), 'wait': True,
    }).result(8)
    assert not response.is_error, response
    assert response.structured_content['status'] == 'succeeded'
    assert rc._worker.config['params']['message'] == 'guarded'


def test_receipt_keeps_its_completion_generation_after_later_intervention(example_rc):
    rc = example_rc
    identity = rc.submit('take', origin='mcp', expected_control=guard(rc))
    completed = rc.wait_operation(identity, 5)
    assert completed['completion_control'] == guard(rc)
    with rc._lock:
        rc.buffer.invalidate()
    later = rc.get_operation(identity)
    assert later['completion_control'] == completed['completion_control']
    assert later['completion_control'] != guard(rc)
