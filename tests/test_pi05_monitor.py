from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from rc_policy_sdk.monitor import normalize_snapshot
from test_pi05_task_controls import running, step
from test_pi05_task_worker import running as running_worker
from test_controller import until
from test_example_controller import command
from qwen_helpers import wait


def test_monitor_tracks_pending_prediction_without_collecting_it(running):
    _, entry, _, policy, model = running
    def snapshot():
        return normalize_snapshot(entry.monitor(policy))

    initial = snapshot()
    assert initial['task'] == 'pick up the object'
    assert initial['chunk_size'] == 50 and initial['request_when_remaining'] == 30
    assert initial['inference_count'] == initial['pending'] == 0
    assert initial['left_gripper_raw'] == initial['right_gripper_raw'] == '[]'
    assert 'predict_ms' not in initial and 'history_frames' not in initial
    left = [1.25, 3.5] * 25
    right = [2.5, -0.25] * 25
    model.output[:, 6], model.output[:, 13] = left, right
    chunk = step(policy, [], 0)
    model.output[:, 6], model.output[:, 13] = right, left
    model.started.clear()
    model.gate.clear()
    chunk = step(policy, chunk[21:], 21)
    assert model.started.wait(1)
    with ThreadPoolExecutor(1) as caller:
        try:
            busy = caller.submit(snapshot).result(1)
            assert busy['inference_busy'] == busy['pending'] == 1
            assert busy['inference_count'] == 2 and busy['cached_actions'] == 29
            assert busy['pending_remaining_ms'] > 0
            assert (busy['left_gripper_raw'], busy['right_gripper_raw']) == (str(left), str(right))
        finally:
            model.gate.set()
    policy.pending.result(2)
    ready = snapshot()
    assert ready['pending'] == 1 and ready['inference_busy'] == 0
    assert ready['inference_completed'] == 2 and ready['predict_ms'] > 0
    assert (ready['left_gripper_raw'], ready['right_gripper_raw']) == (str(right), str(left))
    assert ready['head_image_shape'] and ready['predict_max_ms'] >= ready['predict_avg_ms']
    before = policy.canonical.copy()
    ready['cached_actions'] = -1
    assert snapshot()['cached_actions'] == 29
    np.testing.assert_array_equal(policy.canonical, before)
    remaining = step(policy, chunk[2:], 23)
    assert snapshot()['last_trim'] == snapshot()['trimmed_actions_total'] == 2
    model.error = RuntimeError('observed model error')
    with pytest.raises(RuntimeError, match='observed model error'):
        step(policy, [], 23 + len(remaining))
    failed = snapshot()
    assert failed['context_state'] == 'failed' and failed['inference_errors'] == 1
    assert 'observed model error' in failed['last_error']
    assert (failed['left_gripper_raw'], failed['right_gripper_raw']) == (str(right), str(left))
    policy.reset()
    clean = snapshot()
    assert clean['cached_actions'] == clean['pending'] == 0
    assert clean['reset_count'] == 1 and clean['inference_completed'] == 2
    assert clean['left_gripper_raw'] == clean['right_gripper_raw'] == '[]'


def test_monitor_discovery_and_snapshots_through_worker(running_worker):
    _, rc, _ = running_worker
    worker = rc._worker
    assert worker.supports_monitor and worker.submit_monitor(1)
    assert wait(worker.poll_monitor)['result']['context_state'] == 'ready'
    command(rc, 'resume')
    until(lambda: rc.buffer.consumed >= 3)
    command(rc, 'pause')
    assert worker.submit_monitor(2)
    result = wait(worker.poll_monitor)
    assert result['type'] == 'monitor_result'
    assert result['result']['inference_completed'] >= 1
    assert result['result']['left_gripper_raw'] == str([2.5] * 50)
    assert result['result']['right_gripper_raw'] == str([2.] * 50)


def test_backend_without_model_details_only_reports_observed_metrics(running):
    _, entry, _, existing, model = running
    settings = {key: existing.config[key] for key in ('execution', 'runtime')}
    policy = entry.AsyncPi05Policy(settings, backend=type(model)())
    try:
        step(policy, [], 0)
        values = normalize_snapshot(entry.monitor(policy))
        assert values['predict_ms'] > 0
        assert {'checkpoint', 'model_dtype', 'num_inference_steps', 'processor_ms',
                'model_forward_ms', 'post_ms', 'subtask', 'history_frames'}.isdisjoint(values)
    finally:
        policy.close()
