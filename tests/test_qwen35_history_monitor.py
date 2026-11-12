"""Readonly execution snapshots for both history policy sources; no resource probes."""
from concurrent.futures import ThreadPoolExecutor
import sys

import numpy as np
import pytest

from rc_policy_sdk.monitor import normalize_snapshot
from test_qwen35_history_runtime import Backend, config, implementation, step
from test_qwen35_history_lift2 import entry
from test_qwen35_history_lift2 import running_lift2
from test_qwen35_history_worker import running, completed
from qwen_helpers import wait


@pytest.mark.parametrize('lift2', [False, True])
def test_monitor_reports_configuration_counters_history_and_errors_without_changing_actions(lift2):
    cls = entry().HistoryPolicy if lift2 else implementation()
    backend = Backend()
    settings = config(x=2)
    settings['model'] = {'checkpoint': '/models/history-test'}
    policy = cls(settings, backend=backend)
    try:
        initial = normalize_snapshot(policy.monitor())
        assert initial['robot_type'] == ('arx_lift2' if lift2 else 'arx_acone')
        assert initial['checkpoint'] == '/models/history-test'
        assert initial['task'] == 'untangle'
        assert initial['context_state'] == 'ready'
        assert initial['pipelined'] == 0 and initial['history_enabled'] == 1
        assert initial['history_capacity_frames'] == 481
        chunk = step(policy, [], 0)
        before = policy.canonical.copy()
        snapshot = normalize_snapshot(policy.monitor())
        assert snapshot['inference_count'] == snapshot['inference_completed'] == 1
        assert snapshot['inference_errors'] == snapshot['reset_count'] == 0
        assert snapshot['predict_avg_ms'] > 0 and snapshot['predict_max_ms'] > 0
        assert snapshot['head_image_shape'] == '2x3x3'
        assert snapshot['pending_age_ms'] == snapshot['pending_remaining_ms'] == 0
        assert snapshot['load_ms'] >= 0 and snapshot['warmup_ms'] >= 0
        snapshot['inference_count'] = 999
        assert policy.monitor()['inference_count'] == 1
        np.testing.assert_array_equal(policy.canonical, before)
        step(policy, chunk[1:], 1)
        assert policy.monitor()['history_frames'] == 1
        policy.reset()
        reset = policy.monitor()
        assert reset['reset_count'] == 1 and reset['inference_count'] == 1
        assert reset['cached_actions'] == reset['history_frames'] == 0
        backend.error = RuntimeError('model failure for monitor test')
        with pytest.raises(RuntimeError, match='model failure'):
            step(policy, [], 0)
        failed = normalize_snapshot(policy.monitor())
        assert failed['context_state'] == 'failed'
        assert failed['inference_errors'] == 1 and 'model failure' in failed['last_error']
        assert len(failed) <= 128
        assert not any(word in key for key in failed for word in ('memory', 'cpu', 'rss', 'allocated', 'reserved', 'process_pid'))
    finally:
        policy.close()


def test_monitor_stays_responsive_while_inference_is_blocked_and_does_not_collect():
    backend = Backend()
    policy = implementation()(config(), backend=backend)
    backend.gate.clear()
    with ThreadPoolExecutor(max_workers=1) as caller:
        running = caller.submit(step, policy, [], 0)
        try:
            assert backend.started.wait(1)
            snapshot = normalize_snapshot(policy.monitor())
            assert snapshot['pending'] == snapshot['inference_busy'] == 1
            assert snapshot['pending_age_ms'] >= 0 and snapshot['pending_remaining_ms'] > 0
            assert snapshot['inference_count'] == 1 and snapshot['inference_completed'] == 0
            assert not running.done() and len(backend.calls) == 1
        finally:
            backend.gate.set()
            running.result(2)
            policy.close()


def test_backend_monitor_is_a_detached_scalar_snapshot_without_any_torch_calls(monkeypatch):
    from rc_qwen35_history.backend import HistoryBackend
    class NoTorchCalls:
        def __getattr__(self, name):
            raise AssertionError('monitor must not call Torch: '+name)
    settings = config()
    settings['model'] = {'checkpoint': '/models/test'}
    backend = HistoryBackend(settings, robot_type='arx_lift2')
    monkeypatch.setitem(sys.modules, 'torch', NoTorchCalls())
    snapshot = normalize_snapshot(backend.monitor())
    assert snapshot['model_stage'] == 'not_loaded'
    assert snapshot['denoise_graph_ready'] == snapshot['prefix_graph_layouts'] == 0
    assert snapshot['robot_type'] == 'arx_lift2'
    snapshot['model_stage'] = 'mutated'
    assert backend.monitor()['model_stage'] == 'not_loaded'


def test_failed_initialization_keeps_original_error_and_reports_stage(caplog):
    backend = Backend()
    error = RuntimeError('simulated model initialization failure')
    def fail(): raise error
    backend.load = fail
    backend.monitor = lambda: {'model_stage': 'checkpoint_loading'}
    with pytest.raises(RuntimeError) as caught:
        implementation()(config(), backend=backend)
    assert caught.value is error
    assert 'stage=checkpoint_loading' in caplog.text
    assert any(name == 'close' for name, _ in backend.events)


def test_backend_monitor_failure_does_not_mask_inference_or_original_error():
    backend = Backend()
    def fail_monitor(): raise RuntimeError('monitor-only failure')
    backend.monitor = fail_monitor
    policy = implementation()(config(), backend=backend)
    try:
        result = step(policy, [], 0)
        assert result.shape == (25, 14)
        snapshot = normalize_snapshot(policy.monitor())
        assert 'monitor-only failure' in snapshot['backend_monitor_error']
        assert snapshot['context_state'] == 'ready' and snapshot['inference_errors'] == 0
    finally:
        policy.close()


def assert_worker_monitor(controller, robot_type):
    worker = controller._worker
    assert worker.submit_monitor(1)
    first = wait(worker.poll_monitor)
    assert first['type'] == 'monitor_result', first
    assert first['result']['robot_type'] == robot_type
    assert first['result']['context_state'] == 'ready'
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 5)
    assert completed(controller, 'pause')['status'] == 'succeeded'
    assert worker.submit_monitor(2)
    later = wait(worker.poll_monitor)
    assert later['type'] == 'monitor_result', later
    snapshot = normalize_snapshot(later['result'])
    assert snapshot['inference_count'] >= 1 and snapshot['inference_completed'] >= 1
    assert snapshot['predict_avg_ms'] > 0 and snapshot['head_image_shape'] == '4x5x3'
    assert worker is controller._worker and worker.runnable


def test_original_history_monitor_through_real_worker(running):
    controller, _ = running
    assert_worker_monitor(controller, 'arx_acone')


def test_lift2_history_monitor_through_real_worker(running_lift2):
    controller, _ = running_lift2
    assert_worker_monitor(controller, 'arx_lift2')
