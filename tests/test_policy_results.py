"""A policy episode outlives the MCP request, but never an ownership epoch."""
import copy
import threading

import pytest

from test_controller import setup_controller as setup_controller, ready, until
from test_control_ownership import call
from test_runtime_pause_controller import finish as respond


def begin(controller):
    assert call(controller, 'take')['status'] == 'succeeded'
    identity = controller.submit_policy_wait()
    until(lambda: controller.status()['state'] == 'running')
    return identity


def report(worker, *, outcome='completed', message='任务结果', request=None, **extra):
    respond(worker, pause=False, request=request, control_requests=[
        {'type': 'finish', 'outcome': outcome, 'message': message}], **extra)


@pytest.mark.parametrize('mode', ['qpos', 'epos', 'mixed'])
@pytest.mark.parametrize('scheduling', ['sync', 'async'])
@pytest.mark.parametrize('waiting', [False, True])
def test_result_discards_actions_and_conditionally_returns_control(setup_controller, mode, scheduling, waiting):
    controller, robot, _ = setup_controller
    config = copy.deepcopy(controller.registry.get('first').config)
    config['execution'].update(mode=scheduling, control_mode=mode)
    controller.registry.commit('first', config)
    controller.registry.save('first')
    worker = ready(controller)
    identity = begin(controller) if waiting else controller.submit('resume')
    until(lambda: len(worker.requests) == 1)
    assert controller.get_operation(identity)['status'] == ('running' if waiting else 'succeeded')
    with controller._lock:
        before = controller.buffer.consumed
        held = controller.limiter.commands()
        report(worker, outcome='terminated', modes=['epos' if mode != 'qpos' else 'qpos'] * 8)
    until(lambda: controller.status()['state'] == 'paused')
    assert controller.status()['control_owner'] == ('mcp' if waiting else 'policy')
    assert controller.buffer.consumed == before
    assert controller.status()['queue_remaining'] == 0
    assert controller.limiter.commands() == held
    assert worker is controller._worker and not worker.closed
    assert not controller.status()['policy_wait_operation_id']
    if waiting:
        result = controller.get_policy_result(identity)
        assert result['finished'] and result['status'] == 'succeeded'
        assert result['result'] == {'outcome': 'terminated', 'message': '任务结果', 'source': {'policy_id': 'first'}}
        assert controller.get_policy_result() == result
    else:
        assert controller.get_policy_result()['status'] == 'none'
    assert any('任务结果' in event['message'] for event in controller.read_events()['events'])


def test_wait_does_not_complete_on_an_ordinary_inference_and_rejects_duplicates(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    identity = begin(controller)
    until(lambda: len(worker.requests) == 1)
    resets = worker.resets
    with pytest.raises(RuntimeError) as duplicate:
        controller.submit_policy_wait()
    assert duplicate.value.operation_id == identity
    worker.finish([[.1] * 14] * 4)
    until(lambda: len(worker.requests) == 2)
    assert controller.get_policy_result(identity)['finished'] is False
    assert worker.resets == resets
    assert controller.status()['control_owner'] == 'policy'
    report(worker)
    result = controller.wait_operation(identity, 2)
    assert result['status'] == 'succeeded'


@pytest.mark.parametrize('action', ['pause', 'take', 'reset', 'resume', 'switch second', 'save manual'])
def test_manual_intervention_cancels_wait_and_fences_late_finish(setup_controller, action):
    controller, _, _ = setup_controller
    worker = ready(controller)
    identity = begin(controller)
    until(lambda: len(worker.requests) == 1)
    request = worker.requests[-1]
    intervention = controller.submit(action)
    until(lambda: controller.get_operation(identity)['status'] == 'cancelled')
    assert controller.get_policy_result(identity)['reason'] == '人工接管'
    report(worker, request=request)
    controller.wait_operation(intervention, 2)
    assert controller.get_policy_result(identity)['status'] == 'cancelled'
    assert controller.status()['control_owner'] == ('mcp' if action == 'take' else 'policy')


def test_runtime_pause_cancels_wait_with_manual_reason(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    identity = begin(controller)
    until(lambda: worker.requests)
    respond(worker)
    assert controller.wait_operation(identity, 2)['status'] == 'cancelled'
    assert controller.get_policy_result(identity)['reason'] == '人工接管'
    assert controller.status()['control_owner'] == 'policy'


@pytest.mark.parametrize('failure', ['infer', 'dead', 'bad_result', 'feedback'])
def test_failure_returns_mcp_and_still_cleans_worker(setup_controller, monkeypatch, failure):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    identity = begin(controller)
    until(lambda: worker.requests)
    with controller._lock:
        if failure == 'infer':
            worker.responses.put({'type': 'error', 'request_id': worker.requests[-1]['request_id'], 'error': 'inference timeout'})
        elif failure == 'dead':
            worker.alive = False
        elif failure == 'bad_result':
            report(worker, actions=[])
        else:
            monkeypatch.setattr(robot, 'read_state', lambda: (_ for _ in ()).throw(RuntimeError('feedback lost')))
    receipt = controller.wait_operation(identity, 2)
    assert receipt['status'] == 'failed', receipt
    assert controller.get_policy_result(identity)['reason']
    assert controller.status()['control_owner'] == 'mcp'
    until(lambda: worker.closed)
    assert controller.get_policy_result(identity)['status'] == 'failed'


def test_reset_failure_returns_mcp_and_preserves_failure_reason(setup_controller, monkeypatch):
    controller, _, _ = setup_controller
    worker = ready(controller)
    monkeypatch.setattr(worker, 'reset_context', lambda **kw: (_ for _ in ()).throw(RuntimeError('reset failed')))
    assert call(controller, 'take')['status'] == 'succeeded'
    identity = controller.submit_policy_wait()
    assert controller.wait_operation(identity, 2)['status'] == 'failed'
    assert 'reset failed' in controller.get_policy_result(identity)['reason']
    assert controller.status()['control_owner'] == 'mcp'
    until(lambda: worker.closed)


def test_wait_preconditions_and_query_validation(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    assert controller.get_policy_result()['status'] == 'none'
    with pytest.raises(PermissionError):
        controller.submit_policy_wait()
    ordinary = call(controller, 'take')['operation_id']
    with pytest.raises(ValueError):
        controller.get_policy_result(ordinary)
    with pytest.raises(ValueError):
        controller.get_policy_result('unknown')
    with controller._lock:
        worker.alive = False
        with pytest.raises(RuntimeError):
            controller.submit_policy_wait()
        assert controller.status()['control_owner'] == 'mcp'
        assert not controller.status()['returning']


def test_queued_wait_is_cancelled_by_earlier_take_without_resuming(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    call(controller, 'take')
    # An earlier queued pause must win over a subsequently registered wait.
    with controller._lock:
        pause = controller.submit('pause')
        identity = controller.submit_policy_wait()
    controller.wait_operation(pause, 2)
    assert controller.wait_operation(identity, 2)['status'] == 'cancelled'
    assert worker.resets == 0 and not worker.requests
    assert controller.status()['control_owner'] == 'mcp'
    assert not controller.status()['returning']


def test_exit_ends_wait_without_turning_it_into_manual_takeover(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    identity = begin(controller)
    controller.submit('exit')
    assert controller.done.wait(3)
    result = controller.get_policy_result(identity)
    assert result['status'] == 'cancelled'
    assert result['reason'] != '人工接管'
    assert not controller.status()['policy_wait_operation_id']


@pytest.mark.parametrize('failure', ['worker_died', 'gate_failed'])
def test_handoff_failure_after_acceptance_returns_mcp(setup_controller, monkeypatch, failure):
    controller, _, _ = setup_controller
    worker = ready(controller)
    call(controller, 'take')
    with controller._lock:
        identity = controller.submit_policy_wait()
        if failure == 'worker_died':
            worker.alive = False
        else:
            def gate_failed(*args):
                raise RuntimeError('gate failed')
            monkeypatch.setattr(controller, '_gate_policy_commands', gate_failed)
    assert controller.wait_operation(identity, 2)['status'] == 'failed'
    assert controller.status()['control_owner'] == 'mcp'
    assert not controller.status()['returning']
    monkeypatch.undo()


def test_old_failure_during_handoff_fails_wait_and_preserves_cleanup(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit('resume')
    until(lambda: worker.requests)
    old = worker.requests[-1]
    call(controller, 'take')
    identity = controller.submit_policy_wait()
    until(lambda: controller.status()['state'] == 'resuming')
    worker.responses.put({'type': 'error', 'request_id': old['request_id'], 'error': 'old request failed'})
    assert controller.wait_operation(identity, 2)['status'] == 'failed'
    assert controller.get_policy_result(identity)['reason'] == 'old request failed'
    assert controller.status()['control_owner'] == 'mcp'
    until(lambda: worker.closed)


def test_stale_finish_cannot_resolve_a_new_episode(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    first = begin(controller)
    until(lambda: worker.requests)
    stale = worker.requests[-1]
    call(controller, 'take')
    second = controller.submit_policy_wait()
    until(lambda: controller.status()['state'] == 'resuming')
    report(worker, request=stale, message='old')
    until(lambda: len(worker.requests) == 2)
    report(worker, request=stale, message='duplicate old')
    assert controller.get_policy_result(second)['finished'] is False
    report(worker, message='new')
    assert controller.wait_operation(second, 2)['result']['message'] == 'new'
    assert controller.get_policy_result(first)['reason'] == '人工接管'


def test_resume_feedback_failure_preserves_publication_guard(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    call(controller, 'take')
    def reset(**kwargs):
        monkeypatch.setattr(robot, 'read_state', lambda: (_ for _ in ()).throw(RuntimeError('no fresh feedback')))
    monkeypatch.setattr(worker, 'reset_context', reset)
    identity = controller.submit_policy_wait()
    assert controller.wait_operation(identity, 2)['status'] == 'failed'
    assert controller.status()['control_owner'] == 'mcp'
    assert controller._publish_fault
    assert not worker.closed
    with pytest.raises(RuntimeError, match='publication fault'):
        controller.submit_direct([])


def test_hot_patch_keeps_episode_and_uses_confirmed_version(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    identity = begin(controller)
    until(lambda: worker.requests)
    patch = controller.submit('set policy.params.value 2')
    until(lambda: controller._operation is not None)
    worker.finish([[.1] * 14] * 4)
    assert controller.wait_operation(patch, 2)['status'] == 'succeeded'
    until(lambda: len(worker.requests) == 2)
    assert worker.requests[-1]['config_version'] == 2
    report(worker)
    assert controller.wait_operation(identity, 2)['status'] == 'succeeded'


def test_expired_result_is_not_silently_replaced_by_another_receipt(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.operations.history = 1
    identity = begin(controller)
    until(lambda: worker.requests)
    report(worker)
    assert controller.wait_operation(identity, 2)['status'] == 'succeeded'
    call(controller, 'status')
    with pytest.raises(ValueError, match='expired'):
        controller.get_policy_result(identity)
    with pytest.raises(ValueError, match='expired'):
        controller.get_policy_result()


def test_ordinary_return_still_waits_only_for_handoff_during_policy_wait(setup_controller, monkeypatch):
    controller, _, _ = setup_controller
    worker = ready(controller)
    call(controller, 'take')
    entered, release = threading.Event(), threading.Event()
    reset = worker.reset_context
    def blocked_reset(**kwargs):
        entered.set()
        assert release.wait(3)
        reset(**kwargs)
    monkeypatch.setattr(worker, 'reset_context', blocked_reset)
    try:
        episode = controller.submit_policy_wait()
        assert entered.wait(2)
        handoff = controller.submit('return', origin='mcp')
        assert handoff != episode, 'ordinary return must not reuse the business result receipt'
        release.set()
        result = controller.wait_operation(handoff, 2)
        assert result['status'] == 'succeeded'
        assert result['result']['control_owner'] == 'policy'
        assert controller.get_policy_result(episode)['status'] == 'running'
        until(lambda: worker.requests)
        report(worker)
        assert controller.wait_operation(episode, 2)['result']['outcome'] == 'completed'
    finally:
        release.set()


def test_mcp_internal_command_does_not_strand_a_plain_terminal_command(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    identity = controller.submit('return_and_wait')
    result = controller.wait_operation(identity, .5)
    assert result['status'] == 'failed', result
    assert controller.status()['control_owner'] == 'policy'
    assert not controller.status()['policy_wait_operation_id']
