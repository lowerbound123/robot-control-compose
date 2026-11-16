"""Visual history survives resume and clears on an explicit completed reset."""
import importlib
import threading

import pytest

from test_controller import setup_controller, ready, until
from test_qwen35_history_runtime import Backend, config, frames, step
from test_qwen35_history_worker import running, completed, events
from test_qwen35_history_lift2 import running_lift2
from qwen_helpers import wait


@pytest.mark.parametrize('package', ['rc_qwen35_history', 'rc_qwen35_history_lift2'])
def test_resume_preserves_visual_history_but_reset_clears_it(package):
    cls = importlib.import_module(package + '.runtime').HistoryPolicy
    backend = Backend()
    policy = cls(config(), backend=backend)
    try:
        step(policy, [], 0)
        step(policy, [], 16, image=frames(7))
        saved = tuple(policy.history.frames)
        assert saved
        resets = sum(name == 'reset' for name, _ in backend.events)
        policy.resume()
        assert tuple(policy.history.frames) == saved
        assert policy.history.tick == 16
        assert len(policy.canonical) == 0
        step(policy, [], 16, epoch=2)
        assert backend.calls[-1][3]
        assert sum(name == 'reset' for name, _ in backend.events) == resets
        policy.reset()
        assert not policy.history.frames and policy.history.tick == 0
        assert policy.monitor()['history_frames'] == 0
        step(policy, [], 16, epoch=3)
        assert backend.calls[-1][3] == ()
        assert sum(name == 'load' for name, _ in backend.events) == 1
    finally:
        policy.close()


@pytest.mark.parametrize('interrupt', [None, 'pause', 'take'])
def test_reset_receipt_waits_for_context_clear_and_stays_paused(setup_controller, interrupt):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    worker.supports_resume_context = True
    entered, finish = threading.Event(), threading.Event()
    def clear(cancel_event=None):
        assert robot.commands, 'reset target must publish before history clears'
        entered.set()
        assert finish.wait(3)
        worker.resets += 1
    worker.reset_motion_context = clear
    try:
        identity = controller.submit('reset')
        assert entered.wait(2)
        assert controller.get_operation(identity)['status'] == 'running'
        assert controller.status()['state'] != 'running'
        if interrupt == 'take':
            # Existing MCP admission rejects take during a lifecycle operation.
            with pytest.raises(RuntimeError, match='busy'):
                controller.submit_control('take')
        elif interrupt == 'pause':
            controller.submit('pause')
            until(lambda: controller._operation['cancel'].is_set())
        finish.set()
        until(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'cancelled'))
        assert controller.get_operation(identity)['status'] == ('cancelled' if interrupt == 'pause' else 'succeeded')
        assert controller.status()['state'] == 'paused'
        assert controller.control_owner == 'policy'
        assert worker.resets == 1
    finally:
        finish.set()


def test_resume_dispatches_optional_resume_callback(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    resumed = threading.Event()
    worker.resume_context = lambda cancel_event=None: resumed.set()
    identity = controller.submit('resume')
    until(lambda: controller.get_operation(identity)['status'] == 'succeeded')
    assert resumed.is_set() and worker.resets == 0
    if worker.requests:
        worker.finish([[0.] * 14])


def test_failed_context_clear_does_not_report_reset_success(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    worker.supports_resume_context = True
    def fail(cancel_event=None):
        raise RuntimeError('history clear failed')
    worker.reset_motion_context = fail
    identity = controller.submit('reset')
    until(lambda: controller.get_operation(identity)['status'] == 'failed')
    assert 'history clear failed' in controller.get_operation(identity)['error']
    until(lambda: controller._worker is None)
    assert controller.status()['state'] == 'fault'


def test_mcp_owned_reset_keeps_control_and_clears_before_success(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    worker.supports_resume_context = True
    worker.reset_motion_context = worker.reset_context
    assert controller.wait_operation(controller.submit_control('take'), 2)['status'] == 'succeeded'
    identity = controller.submit('reset', origin='mcp')
    assert controller.wait_operation(identity, 2)['status'] == 'succeeded'
    assert worker.resets == 1
    assert controller.control_owner == 'mcp' and controller.status()['state'] == 'paused'


def test_explicit_reset_waits_for_full_publication_before_context_clear(setup_controller, monkeypatch):
    from test_connector_control import delayed_robot, receipt
    controller, robot, _ = setup_controller
    submitted, receipts = delayed_robot(monkeypatch, robot)
    worker = ready(controller)
    worker.supports_resume_context = True
    cleared = threading.Event()
    worker.reset_motion_context = lambda cancel_event=None: cleared.set()
    identity = controller.submit('reset')
    until(lambda: controller._motion is not None and controller._motion.get('awaiting_publication'))
    publication = controller._motion['awaiting_publication']
    assert not cleared.is_set() and controller.get_operation(identity)['status'] == 'running'
    receipts.append(receipt(publication))
    until(lambda: controller.get_operation(identity)['status'] == 'succeeded')
    assert cleared.is_set() and controller.status()['state'] == 'paused'


def test_lifecycle_superseding_motion_cancels_reset_without_replacing_job(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    worker.supports_resume_context = True
    with controller._lock:
        identity = controller.operations.create('reset')
        controller.operations.update(identity, 'running')
        motion = {'source': 'reset', 'reset_policy_context': True, 'public_id': identity}
        competing = {'kind': 'switch'}
        controller._motion = motion
        controller._operation = competing
        try:
            controller._complete_motion(motion)
            assert controller._operation is competing
            assert controller.get_operation(identity)['status'] == 'cancelled'
            assert worker.resets == 0
        finally:
            controller._operation = controller._motion = None


def exercise_history_worker(deployment):
    controller, directory = deployment
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 32
         and any(row['indices'] for row in events(directory, 'predict_end')), timeout=10)
    assert completed(controller, 'pause')['status'] == 'succeeded'
    wait(lambda: controller._inflight is None and controller._observe_inflight is None)
    predictions = len(events(directory, 'predict_end'))
    resets = len(events(directory, 'reset'))
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: len(events(directory, 'predict_end')) > predictions)
    assert events(directory, 'predict_end')[predictions]['indices']
    assert len(events(directory, 'reset')) == resets
    identity = controller.submit_control('take')
    assert controller.wait_operation(identity, 5)['status'] == 'succeeded'
    wait(lambda: controller._inflight is None and controller._observe_inflight is None)
    predictions = len(events(directory, 'predict_end'))
    identity = controller.submit_control('return')
    assert controller.wait_operation(identity, 5)['status'] == 'succeeded'
    wait(lambda: len(events(directory, 'predict_end')) > predictions)
    assert events(directory, 'predict_end')[predictions]['indices']
    assert len(events(directory, 'reset')) == resets
    assert completed(controller, 'reset')['status'] == 'succeeded'
    assert controller.status()['state'] == 'paused'
    assert len(events(directory, 'reset')) == resets + 1
    predictions = len(events(directory, 'predict_end'))
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: len(events(directory, 'predict_end')) > predictions)
    assert events(directory, 'predict_end')[predictions]['indices'] == []
    assert len(events(directory, 'load')) == 1


def test_acone_worker_preserves_history_until_explicit_reset(running):
    exercise_history_worker(running)


def test_lift2_worker_preserves_history_until_explicit_reset(running_lift2):
    exercise_history_worker(running_lift2)
