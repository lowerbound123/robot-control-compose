"""Pause effects must be consumed by the control thread before installing actions."""
import copy

import numpy as np
import pytest

from test_controller import setup_controller as setup_controller, ready, until
from test_chain_controller import native_controller as native_controller, operation


def finish(worker, *, pause=True, modes=None, request=None, actions=None, **extra):
    request = request or worker.requests[-1]
    response = {'type': 'result', 'images_released': True, **{key: request[key] for key in ('request_id', 'generation', 'config_version')},
                'actions': actions if actions is not None else [[.8] * 14] * 8, **extra}
    if pause:
        response['control_requests'] = [{'type': 'pause', 'reason': 'task complete'}]
    if modes is not None:
        response['modes'] = modes
    worker.responses.put(response)


@pytest.mark.parametrize('scheduling', ['sync', 'async'])
@pytest.mark.parametrize('mode', ['qpos', 'epos', 'mixed'])
def test_pause_result_never_installs_or_trims_and_retains_worker(setup_controller, monkeypatch, scheduling, mode):
    controller, robot, _ = setup_controller
    config = copy.deepcopy(controller.registry.get('first').config)
    config['execution'].update(mode=scheduling, control_mode=mode)
    controller.registry.commit('first', config)
    controller.registry.save('first')
    worker = ready(controller)
    accepted = []
    accept = controller.buffer.accept

    def track_accept(token, actions):
        accepted.append(token)
        return accept(token, actions)

    monkeypatch.setattr(controller.buffer, 'accept', track_accept)
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 1)
    action_mode = 'epos' if mode != 'qpos' else 'qpos'
    finish(worker, pause=False, actions=[[.1] * 14] * 100, modes=[action_mode] * 100)
    until(lambda: len(worker.requests) == 2)
    consumed = controller.status()['consumed']
    if scheduling == 'async':
        until(lambda: controller.status()['consumed'] > consumed)
    with controller._lock:
        held = copy.deepcopy(controller.limiter.commands())
        consumed = controller.buffer.consumed
        generation = controller.buffer.generation
        finish(worker, modes=[action_mode] * 8)
    until(lambda: controller.status()['state'] == 'paused')
    assert controller.status()['consumed'] == consumed
    assert controller.status()['generation'] == generation + 1
    assert controller.status()['queue_remaining'] == 0
    assert not controller.status()['in_flight']
    assert controller.limiter.commands() == held
    assert len(accepted) == 1, 'pause result must bypass buffer.accept, including async trimming'
    assert worker is controller._worker and worker.alive and not worker.closed
    assert len(worker.requests) == 2
    assert not any(np.allclose(command, .8) for command in robot.commands)
    assert any('task complete' in event['message'] for event in controller.read_events()['events'])
    resets = worker.resets
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 3)
    assert worker.resets == resets + 1
    assert worker is controller._worker


@pytest.mark.parametrize('action', ['pause', 'take', 'switch second', 'exit'])
def test_late_pause_result_cannot_override_manual_control_or_lifecycle(setup_controller, action):
    controller, _, _ = setup_controller
    worker = ready(controller)
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 1)
    request = worker.requests[-1]
    operation(controller, controller.submit(action))
    before = controller.status()
    finish(worker, request=request)
    if action not in ('exit', 'switch second'):
        until(lambda: not controller.status()['in_flight'])
    after = controller.status()
    assert (after['state'], after['control_owner'], after['policy'], after['generation']) == (
        before['state'], before['control_owner'], before['policy'], before['generation'])
    assert not any('task complete' in event['message'] for event in controller.read_events()['events'])


@pytest.mark.parametrize('invalid', ['actions', 'generation', 'config_version', 'controls'])
def test_pause_never_masks_invalid_result(setup_controller, invalid):
    controller, _, _ = setup_controller
    worker = ready(controller)
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 1)
    with controller._lock:
        finish(worker)
        response = worker.responses.get_nowait()
        if invalid == 'controls':
            response['control_requests'] = [{'type': 'resume', 'reason': 'bad'}]
        else:
            response[invalid] = [] if invalid == 'actions' else 999
        worker.responses.put(response)
    until(lambda: controller.status()['state'] == 'fault')
    assert controller.status()['consumed'] == 0
    assert not any('task complete' in event['message'] for event in controller.read_events()['events'])


def test_pause_preserves_a_pending_hot_patch_and_remains_paused(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 1)
    pending = controller.submit('set policy.params.value 2')
    until(lambda: controller._operation is not None)
    with controller._lock:
        assert controller._operation['kind'] == 'hot_patch'
        assert not controller._operation['launched']
        finish(worker)
    operation(controller, pending)
    assert controller.status()['state'] == 'paused'
    assert controller.status()['config_version'] == 2
    assert controller.registry.get('first').config['params']['value'] == 2
    assert controller.status()['queue_remaining'] == controller.status()['consumed'] == 0
    assert controller._worker is worker and worker.alive


def test_editor_open_invalidates_pause_result_already_waiting_to_be_collected(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    operation(controller, controller.submit('resume'))
    until(lambda: len(worker.requests) == 1)
    with controller._lock:
        finish(worker)
        pending = controller.manage_policy('open')
    result = operation(controller, pending)
    until(lambda: not controller.status()['in_flight'])
    assert controller.status()['state'] == 'paused'
    assert controller._edit_active and result['result']['token'] == controller._edit_serial
    assert not any('task complete' in event['message'] for event in controller.read_events()['events'])
    assert controller._worker is worker and worker.alive


@pytest.mark.parametrize('scheduling', ['sync', 'async'])
def test_real_native_chain_pauses_only_after_final_cell_and_frees_lease(native_controller, monkeypatch, scheduling):
    controller, children, root = native_controller
    events = root / 'calls'
    gate = root / 'finish-chain'
    source = f'''
import json, time
from pathlib import Path
def initialize(config): return {{'calls': 0}}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['calls'] += 1
    with open({str(events)!r}, 'a') as stream:
        stream.write(json.dumps([config['id'], context['calls']]) + '\\n')
    if config['id'] == 'a' and context['calls'] == 1:
        runtime.pause('native complete')
    if config['id'] == 'b':
        while not Path({str(gate)!r}).exists():
            assert images['head'].shape[0] > 0
            time.sleep(.005)
    return [[.9] * 14]
def reset_context(context): context['calls'] = 0
def release(context): pass
def test(context): return context['calls']
def change(context, value): return {{'params.value': value}}
'''
    for identity in ('a', 'b'):
        (root / 'policy' / identity / 'main.py').write_text(source)
        (root / 'policy' / identity / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        config = copy.deepcopy(controller.registry.get(identity).config)
        config['execution']['mode'] = scheduling
        controller.registry.commit(identity, config)
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.manage_policy('apply', ['a', 'b', 'a'], token=token))
    worker = controller._worker
    accepted = []
    accept = controller.buffer.accept
    monkeypatch.setattr(controller.buffer, 'accept', lambda *args: (accepted.append(args), accept(*args))[1])
    operation(controller, controller.submit('resume'))
    try:
        until(lambda: events.exists() and '["b", 1]' in events.read_text())
        assert controller.status()['state'] == 'running'
        with controller._lock:
            lease = controller._inflight['lease']
            assert not lease._released
        gate.touch()
        until(lambda: controller.status()['state'] == 'paused')
        assert events.read_text().splitlines() == ['["a", 1]', '["b", 1]', '["a", 2]']
        assert accepted == [] and controller.status()['consumed'] == 0
        assert lease._released
        assert controller._worker is worker and worker.alive
        assert len([child for child in children if child.alive]) == 2
        assert any('native complete' in event['message'] and 'a' in event['message'] and 'cell 1' in event['message']
                   for event in controller.read_events()['events'])
    finally:
        gate.touch()
