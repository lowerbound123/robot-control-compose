"""A browser plan may outlive a human intervention or an RC restart."""
from test_controller import setup_controller, ready, until


def test_mcp_expected_control_checks_before_enqueue_and_again_before_dispatch(setup_controller):
    from robot_control.mcp_server import MCPService
    rc, _, _ = setup_controller
    ready(rc)
    service = MCPService(rc, {'port': 0}, allow_ephemeral=True)
    service.start()
    try:
        status = rc.status()
        assert status['run_id'] == rc.run_id
        guard = {key: status[key] for key in ('run_id', 'control_epoch', 'generation')}
        reply = service.submit_tool('take', {'expected_control': guard, 'wait': True}).result(3)
        assert not reply.is_error, reply
        before = rc.status()
        denied = service.submit_tool('command', {'line': 'reset', 'expected_control': guard}).result(3)
        assert denied.is_error and 'operation_id' not in denied.structured_content
        assert rc.status()['generation'] == before['generation']
        fresh = {key: before[key] for key in guard}
        with rc._lock:
            queued = rc.submit_control('return', expected_control=fresh)
            rc.buffer.invalidate()  # Intervention after admission, before execution.
        assert rc.wait_operation(queued, 3)['status'] in ('cancelled', 'failed')
        assert rc.control_owner == 'mcp' and not rc.status()['returning']
        invalid_run = {**fresh, 'run_id': 'another-run'}
        denied = service.submit_tool('record', {'action': 'on', 'expected_control': invalid_run}).result(3)
        assert denied.is_error and rc.status()['recording']['state'] == 'idle'
    finally:
        service.close()


def test_guarded_recording_preserves_control_and_accepts_current_generation(setup_controller):
    from robot_control.mcp_server import MCPService
    rc, _, _ = setup_controller
    ready(rc)
    service = MCPService(rc, {'port': 0}, allow_ephemeral=True)
    service.start()
    try:
        before = rc.status()
        guard = {key: before[key] for key in ('run_id', 'control_epoch', 'generation')}
        for action in ('on', 'off'):
            reply = service.submit_tool('record', {'action': action, 'expected_control': guard, 'wait': True}).result(5)
            assert not reply.is_error, reply
        assert all(rc.status()[key] == before[key] for key in (*guard, 'control_owner'))
    finally:
        service.close()
