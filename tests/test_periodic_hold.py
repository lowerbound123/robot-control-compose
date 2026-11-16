"""Waiting changes target selection, not the periodic publication clock."""
import numpy as np
import pytest
import queue
import threading

from test_controller import setup_controller, ready, until
from test_connector_control import delayed_robot, receipt


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_waiting_policy_sends_hold_or_next_action_without_extra_requests(setup_controller, monkeypatch, mode):
    rc, robot, _ = setup_controller
    worker = ready(rc)
    with rc._lock:
        rc.registry.get('first').config['execution'].update(mode=mode, call_every_actions=100)
        rc._state = 'running'
        rc.buffer.install(np.array([[.1]*14, [.2]*14]))
        rc._submit_prediction()
        monkeypatch.setattr(rc, '_observe_tick', lambda: None)
        held = rc.limiter.previous.copy()
        before = len(robot.commands)
        for _ in range(3): rc._tick()
        actual = robot.commands[before:]
        assert len(actual) == 3
        if mode == 'sync':
            np.testing.assert_array_equal(actual, [held]*3)
            assert rc.buffer.consumed == 0 and len(rc.buffer.remaining()) == 2
        else:
            np.testing.assert_allclose(actual, [[.1]*14,[.2]*14,[.2]*14])
            assert rc.buffer.consumed == 2
        assert len(worker.requests) == 1


def test_sync_due_tick_sends_hold_before_preparing_a_new_request(setup_controller, monkeypatch):
    rc, robot, _ = setup_controller
    ready(rc)
    order = []
    with rc._lock:
        rc._state = 'running'
        rc._needs_request = True
        rc.buffer.install(np.array([[.1]*14]))
        monkeypatch.setattr(robot, 'execute', lambda values: order.append('send'))
        monkeypatch.setattr(rc, '_observe_tick', lambda: order.append('observe'))
        monkeypatch.setattr(rc, '_submit_prediction', lambda snapshot=None: order.append('request'))
        rc._tick()
        assert order == ['send', 'observe', 'request']
        assert rc.buffer.consumed == 0 and len(rc.buffer.remaining()) == 1


def test_sync_target_ack_barrier_repeats_hold_but_hold_receipts_do_not_consume(setup_controller, monkeypatch):
    rc, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    ready(rc)
    with rc._lock:
        rc.registry.get('first').config['execution'].update(mode='sync', call_every_actions=100)
        rc._state = 'running'; rc._needs_request = False
        rc.buffer.install(np.array([[.1]*14, [.2]*14]))
        monkeypatch.setattr(rc, '_observe_tick', lambda: None)
        before = len(submitted)
        rc._tick()
        first = submitted[-1][0]
        for _ in range(3): rc._tick()
        rows = submitted[before:]
        assert len(rows) == 4
        np.testing.assert_allclose([row[1] for row in rows], [[.1]*14]*4)
        assert rc.buffer.consumed == 0
        receipts.append(receipt(first))
        assert rc._collect_publications()
        assert rc.buffer.consumed == 1
        # Outstanding holds do not keep the original target barrier closed.
        rc._tick()
        np.testing.assert_allclose(submitted[-1][1], [.2]*14)
        for identity, _, _ in rows[1:]: receipts.append(receipt(identity))
        assert not rc._collect_publications()
        assert rc.buffer.consumed == 1


def test_result_notification_installs_before_next_send_deadline(setup_controller):
    rc, robot, _ = setup_controller
    rc.config['frequency_hz'] = 1
    worker = ready(rc)
    rc.submit('resume')
    until(lambda: bool(worker.requests), 2)
    with rc._lock:
        before = len(robot.commands)
        worker.finish([[.15]*14]*3)
    until(lambda: rc._inflight is None and len(rc.buffer.remaining()) == 3, .3)
    assert len(robot.commands) == before, 'result notification sent an early action'


def test_observation_notification_releases_lease_before_next_tick(setup_controller):
    rc, _, _ = setup_controller
    rc.config['frequency_hz'] = 1
    worker = ready(rc)
    replies = queue.Queue()
    worker.supports_observe = True
    worker.observe = lambda request: True
    def poll():
        try: return replies.get_nowait()
        except queue.Empty: return None
    worker.poll_observation = poll
    with rc._lock:
        rc._state = 'running'
        rc._observe_tick()
        lease = rc._observe_inflight[1]
        assert not lease._released
    replies.put({'type':'observed','images_released':True})
    worker.ready_event.set()
    until(lambda: lease._released, .3)


def test_reader_drain_does_not_busy_spin_on_an_expired_inference_deadline(setup_controller, monkeypatch):
    rc, _, _ = setup_controller
    rc.config['frequency_hz'] = 1
    worker = ready(rc)
    entered, release = threading.Event(), threading.Event()
    waits = []
    original_wait = rc._wake.wait
    def capture(timeout=None):
        if not waits:
            waits.append(timeout)
            entered.set()
            release.wait(1)
        return original_wait(timeout)
    with rc._lock:
        worker.next_result_deadline = 0.
        worker.poll_reader_result = lambda: None
        rc._inflight = {'worker':worker}
        rc._operation = {'kind':'reload_member','in_place':True,'prepared':True,
                         'old_worker':worker,'launched':True}
        monkeypatch.setattr(rc._wake, 'wait', capture)
    rc._wake.set()
    try:
        assert entered.wait(.5)
        assert waits[0] > .1, 'expired inference deadline spun while waiting for real reader release'
    finally:
        with rc._lock:
            rc._inflight = None
            rc._operation = None
        release.set()


def test_waiting_sync_hold_preserves_per_arm_coordinate_modes(setup_controller, monkeypatch):
    rc, robot, _ = setup_controller
    ready(rc)
    with rc._lock:
        rc._state = 'running'
        rc._send_direct({'left': {'mode':'qpos','values':[.1]*7},
                         'right': {'mode':'epos','values':[.2]*7}}, robot.read_state())
        targets = rc.limiter.commands()
        rc._submit_prediction()
        monkeypatch.setattr(rc, '_observe_tick', lambda: None)
        before = len(robot.command_modes)
        for _ in range(3): rc._tick()
        assert robot.command_modes[before:] == [{'left':'qpos','right':'epos'}]*3
        assert rc.limiter.commands() == targets and rc.buffer.consumed == 0


def test_inflight_sync_freezes_partial_slew_then_finishes_protected_target(setup_controller, monkeypatch):
    rc, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    worker = ready(rc)
    with rc._lock:
        rc.registry.get('first').config['execution'].update(mode='sync', call_every_actions=100)
        rc._state = 'running'; rc._needs_request = False
        rc.limiter.enabled = True
        rc.limiter.limits[:] = .05
        goal = rc.limiter.previous.copy()+.2
        rc.buffer.install(np.array([goal]))
        monkeypatch.setattr(rc, '_observe_tick', lambda: None)
        rc._tick()
        assert rc.buffer.submission_state() == 'partial'
        held = submitted[-1][1].copy()
        rc._submit_prediction()
        before = len(submitted)
        for _ in range(3): rc._tick()
        np.testing.assert_array_equal([row[1] for row in submitted[before:]], [held]*3)
        assert rc.buffer.consumed == 0
        worker.finish([goal])
        rc._collect_prediction()
        for _ in range(8):
            if rc.buffer.submission_state() == 'awaiting_publication': break
            rc._tick()
        assert rc.buffer.submission_state() == 'awaiting_publication'
        np.testing.assert_allclose(submitted[-1][1], goal)
        assert rc.buffer.consumed == 0
        for identity, _, _ in submitted: receipts.append(receipt(identity))
        assert rc._collect_publications()
        assert rc.buffer.consumed == 1


def test_hold_publication_failure_stops_submissions_without_consuming(setup_controller, monkeypatch):
    rc, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    ready(rc)
    with rc._lock:
        rc._state = 'running'
        rc._submit_prediction()
        monkeypatch.setattr(rc, '_observe_tick', lambda: None)
        rc._tick()
        receipts.append(receipt(submitted[-1][0], success=False))
        with pytest.raises(RuntimeError, match='failure'): rc._collect_publications()
        before = len(submitted)
        rc._tick()
        assert len(submitted) == before and rc.buffer.consumed == 0 and rc._publish_fault
