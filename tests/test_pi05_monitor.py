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
    assert 'predict_ms' not in initial and 'history_frames' not in initial
    chunk = step(policy, [], 0)
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
        finally:
            model.gate.set()
    policy.pending.result(2)
    ready = snapshot()
    assert ready['pending'] == 1 and ready['inference_busy'] == 0
    assert ready['inference_completed'] == 2 and ready['predict_ms'] > 0
    assert ready['head_image_shape'] and ready['predict_max_ms'] >= ready['predict_avg_ms']
    before = policy.canonical.copy()
    ready['cached_actions'] = -1
    assert snapshot()['cached_actions'] == 29
    np.testing.assert_array_equal(policy.canonical, before)
    step(policy, chunk[2:], 23)
    assert snapshot()['last_trim'] == snapshot()['trimmed_actions_total'] == 2
    policy.reset()
    clean = snapshot()
    assert clean['cached_actions'] == clean['pending'] == 0
    assert clean['reset_count'] == 1 and clean['inference_completed'] == 2
    model.error = RuntimeError('observed model error')
    with pytest.raises(RuntimeError, match='observed model error'):
        step(policy, [], 0)
    failed = snapshot()
    assert failed['context_state'] == 'failed' and failed['inference_errors'] == 1
    assert 'observed model error' in failed['last_error']


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
