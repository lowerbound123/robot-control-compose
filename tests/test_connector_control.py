from collections import deque
import threading

import numpy as np

from test_controller import setup_controller, ready, until


def delayed_robot(monkeypatch, robot):
    submitted, receipts = [], deque()
    robot.asynchronous = True
    original_close = robot.close
    robot.close = lambda **kwargs: original_close()
    def execute(values, mode='qpos'):
        submitted.append((len(submitted) + 1, np.asarray(values).copy(), threading.current_thread().name))
        return len(submitted)
    def poll():
        result = list(receipts)
        receipts.clear()
        return result
    monkeypatch.setattr(robot, 'execute', execute)
    robot.poll_results = poll
    return submitted, receipts


def receipt(identity, success=True):
    return {'id': identity, 'success': success, 'arms': {'left': 'published', 'right': 'published' if success else 'unknown'},
            'error': None if success else 'injected failure', 'events': [], 'feedback_watermarks': {}}


def test_rc_submits_before_ack_and_consumes_only_after_publication(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    worker = ready(controller)
    controller.registry.get('first').config['execution']['call_every_actions'] = 100
    controller.submit('resume')
    until(lambda: worker.requests)
    worker.finish(np.array([[i / 100.] * 14 for i in range(12)]))
    until(lambda: len([row for row in submitted if row[1][6] >= 0]) >= 3)
    targets = [row for row in submitted if row[1][6] >= 0]
    assert controller.buffer.consumed == 0
    np.testing.assert_allclose([v[1][0] for v in targets[:3]], [0, .01, .02])
    assert all(row[2] == 'rc-control' for row in submitted)
    for identity, _, _ in submitted:
        if identity <= targets[0][0]: receipts.append(receipt(identity))
    until(lambda: controller.buffer.consumed == 1)
    controller.wait_operation(controller.submit('take'), 2)
    for identity, _, _ in submitted:
        if identity > targets[0][0]: receipts.append(receipt(identity))
    until(lambda: not receipts)
    assert controller.buffer.consumed == 1
    assert controller.control_owner == 'mcp'


def test_partial_failure_never_consumes_and_stops_new_submissions(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    worker = ready(controller)
    controller.registry.get('first').config['execution']['call_every_actions'] = 100
    controller.submit('resume')
    until(lambda: worker.requests)
    worker.finish(np.ones((12, 14)))
    until(lambda: len(submitted) >= 2)
    receipts.append(receipt(1, False))
    until(lambda: controller.status()['state'] == 'fault')
    assert controller.buffer.consumed == 0
    count = len(submitted)
    receipts.append(receipt(2))
    until(lambda: not receipts)
    assert len(submitted) == count and controller.buffer.consumed == 0


def test_available_target_is_submitted_before_observation_copy(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    ready(controller)
    order = []
    with controller._lock:
        controller.registry.get('first').config['execution'].update(mode='async', call_every_actions=100)
        controller._state = 'running'
        controller._needs_request = False
        controller.buffer.install(np.ones((10, 14)))
        monkeypatch.setattr(controller, '_observe_tick', lambda: order.append('observe'))
        monkeypatch.setattr(robot, 'execute', lambda values: order.append('send'))
        controller._tick()
        assert order[:2] == ['send', 'observe']


def test_paused_controller_still_collects_observation_release(setup_controller):
    from types import SimpleNamespace
    controller, _, _ = setup_controller
    ready(controller)
    released = []
    observer = type('Observer', (), {'poll_observation': lambda self: {'type': 'observation_result', 'images_released': True}})()
    lease = SimpleNamespace(release=lambda: released.append(True))
    with controller._lock:
        controller._observe_inflight = (observer, lease, controller.buffer.generation)
        controller._tick()
        assert released == [True]
        assert controller._observe_inflight is None


def test_exit_stops_new_hold_submissions_while_policy_release_is_pending(setup_controller):
    import time
    controller, robot, _ = setup_controller
    worker = ready(controller)
    entered, finish = threading.Event(), threading.Event()
    original = worker.close
    def close():
        entered.set()
        assert finish.wait(2)
        return original()
    worker.close = close
    try:
        controller.submit('exit')
        assert entered.wait(1)
        count = len(robot.commands)
        time.sleep(.06)
        assert len(robot.commands) == count
    finally: finish.set()
