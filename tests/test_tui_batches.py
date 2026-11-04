"""Only text-batch scheduling; the controller uses a lightweight test worker."""
import importlib.util
import json
import threading

import pytest

from test_controller import setup_controller, ready, until


def parser():
    assert importlib.util.find_spec('robot_control.terminal_batches'), 'text batch parser is missing'
    from robot_control.terminal_batches import parse_batch
    return parse_batch


def test_batch_parser_preserves_line_numbers_json_and_quotes():
    text = '\r\n set example.params.message "a \\"quoted\\" value"\r\n\nstatus '
    assert parser()(text) == [(2, 'set example.params.message "a \\"quoted\\" value"'), (4, 'status')]


@pytest.mark.parametrize('text', ['', ' \n\t', 'x' * 4001, 'status\n' * 65,
    'status\nmcp take', '"mcp" take', 'status\ncamera on', 'monitor', 'monitor-example',
    'manage-policy', 'snapshot', 'set global.api_key "never-log-secret"',
    'status\nset global.x "unclosed', 'exit\nstatus', 'quit\nstatus'])
def test_batch_preflight_rejects_entire_input_without_secret_values(text):
    parse = parser()
    with pytest.raises(ValueError) as error:
        parse(text)
    assert 'never-log-secret' not in str(error.value)


def wait(rc, identity, status='succeeded'):
    reply = rc.wait_operation(identity, 5)
    assert reply['status'] == status, json.dumps(reply)
    return reply


def test_batches_are_fifo_and_internal_tui_semantics_do_not_interrupt(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        a = rc.submit_tui_batch('set policy.params.value 2\nshow policy')
        b = rc.submit_tui_batch('set policy.params.value 3\nshow policy')
    first, second = wait(rc, a), wait(rc, b)
    assert first['result']['steps'][1]['result']['params']['value'] == 2
    assert second['result']['steps'][1]['result']['params']['value'] == 3
    assert rc.control_owner == 'policy'
    assert second['result']['steps'][0]['started_at'] >= first['updated_at']


def test_one_failed_batch_does_not_execute_its_tail_or_cancel_next_batch(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        a = rc.submit_tui_batch('unknown-command\nset policy.params.value 9')
        b = rc.submit_tui_batch('show policy')
    assert wait(rc, a, 'failed')['result']['steps'][1]['status'] == 'cancelled'
    assert wait(rc, b)['result']['steps'][0]['result']['params']['value'] == 1


def test_local_query_cancels_all_waiting_batches(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        ids = [rc.submit_tui_batch('set policy.params.value 9') for _ in range(3)]
        rc.submit('status')
    for identity in ids:
        wait(rc, identity, 'cancelled')
    assert rc.query('show policy')['params']['value'] == 1


def test_local_input_revokes_child_already_in_controller_queue(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        identity = rc.submit_tui_batch('set policy.params.value 9\nstatus')
        rc._tui_batches.poll()
        child = rc.get_operation(identity)['result']['steps'][0]['operation_id']
        rc.submit('status')
    wait(rc, identity, 'cancelled')
    wait(rc, child, 'cancelled')
    until(lambda: rc._commands.empty())
    assert rc.query('show policy')['params']['value'] == 1


def test_local_query_leaves_started_child_to_finish_but_clears_remainder(setup_controller, monkeypatch):
    rc, _, _ = setup_controller
    worker = ready(rc)
    entered, release = threading.Event(), threading.Event()
    update = worker.update_config
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return update(*args, **kwargs)
    monkeypatch.setattr(worker, 'update_config', delayed)
    identity = rc.submit_tui_batch('set policy.params.value 2\nset policy.params.value 9')
    try:
        assert entered.wait(2)
        queued = rc.submit_tui_batch('set policy.params.value 7')
        rc.submit('status')
        assert rc.get_operation(identity)['status'] == 'running'
        wait(rc, queued, 'cancelled')
    finally:
        release.set()
    reply = wait(rc, identity, 'cancelled')
    assert reply['result']['steps'][0]['status'] == 'succeeded'
    assert rc.query('show policy')['params']['value'] == 2


def test_cancel_is_idempotent_and_only_affects_selected_batch(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        a, b = [rc.submit_tui_batch('status') for _ in range(2)]
        rc.cancel_tui_batch(a)
        rc.cancel_tui_batch(a)
    wait(rc, a, 'cancelled')
    wait(rc, b)
    with pytest.raises(ValueError):
        rc.cancel_tui_batch(rc.submit('status'))


def test_batch_limit_and_stale_guard_reject_before_acceptance(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    guard = {k: rc.status()[k] for k in ('run_id', 'control_epoch', 'generation')}
    with rc._lock:
        rc.buffer.invalidate()
        with pytest.raises(PermissionError):
            rc.submit_tui_batch('status', expected_control=guard)
        ids = [rc.submit_tui_batch('status') for _ in range(32)]
        with pytest.raises(RuntimeError, match='full'):
            rc.submit_tui_batch('status')
        rc.submit('status')
    assert all(wait(rc, identity, 'cancelled') for identity in ids)


def test_panel_only_terminal_submission_also_clears_batches(setup_controller):
    from robot_control.terminal_commands import TerminalCommandRouter
    rc, _, _ = setup_controller
    ready(rc)
    router = TerminalCommandRouter(rc)
    with rc._lock:
        identity = rc.submit_tui_batch('status')
        with pytest.raises(ValueError, match='TUI'):
            router.submit('camera on')
    wait(rc, identity, 'cancelled')
    router.close()


def test_batch_exit_receipt_follows_real_shutdown(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    identity = rc.submit_tui_batch('status\nexit')
    assert rc.done.wait(5)
    reply = wait(rc, identity)
    assert reply['result']['steps'][-1]['result']['exit_code'] == 0


def test_tui_interrupt_cannot_undo_exit_already_admitted(setup_controller):
    rc, _, _ = setup_controller
    ready(rc)
    with rc._lock:
        identity = rc.submit_tui_batch('exit')
        rc._tui_batches.poll()  # exit already set _stop before dispatch.
        rc.submit('status')
    assert rc.done.wait(5)
    reply = wait(rc, identity, 'cancelled')
    assert reply['result']['steps'][0]['status'] == 'succeeded'


def test_lifecycle_discarded_child_does_not_leave_batch_source_state(setup_controller):
    rc, _, _ = setup_controller
    worker = ready(rc)
    with rc._lock:
        identity = rc.submit_tui_batch('set first.params.value 2')
        rc._tui_batches.poll()
        rc._gate_policy_commands(worker)  # Existing lifecycle barrier removes the queued request.
        rc._tui_batches.poll()
        assert rc.get_operation(identity)['status'] == 'cancelled'
        assert not rc._tui_batches.queued_children


@pytest.mark.parametrize('router', [False, True])
def test_even_rejected_local_secret_input_interrupts_without_logging_value(setup_controller, router):
    from robot_control.terminal_commands import TerminalCommandRouter
    rc, _, _ = setup_controller
    ready(rc)
    local = TerminalCommandRouter(rc) if router else rc
    with rc._lock:
        identity = rc.submit_tui_batch('status')
        with pytest.raises(ValueError):
            local.submit('set global.api_key "local-secret-canary"')
        assert rc.get_operation(identity)['status'] == 'cancelled'
    assert 'local-secret-canary' not in str(rc.read_events(limit=1000))

