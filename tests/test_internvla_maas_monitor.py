"""MaaS request metrics and the real read-only policy monitor boundary."""
import importlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from internvla_maas_helpers import configured, images, model_server, settings, wait

pytest.importorskip('websockets')
pytest.importorskip('msgpack')
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason='MaaS image uses Python 3.11')

EMPTY = {'avg_latency_ms': '—', 'median_latency_ms': '—', 'last_latency_ms': '—', 'failed_requests': 0}


def metrics_module():
    return importlib.import_module('rc_internvla_maas.metrics')


def make_backend(tmp_path, address, **execution):
    from rc_internvla_maas.backend import MaasBackend
    config = settings(address)
    config['execution'] = execution
    return MaasBackend(configured(tmp_path, config))


def test_exact_cumulative_statistics_odd_even_and_independent_snapshots(monkeypatch):
    module = metrics_module()
    now = [0.]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    metrics = module.RequestMetrics()
    assert metrics.snapshot() == EMPTY
    for elapsed, mean, median in [(0.125, 125., 125.), (0.5, 312.5, 312.5),
                                  (0.25, 875 / 3, 250.), (0.375, 312.5, 312.5)]:
        metrics.start()
        now[0] += elapsed
        metrics.finish(failed=elapsed == .5)
        snapshot = metrics.snapshot()
        assert snapshot['avg_latency_ms'] == pytest.approx(mean)
        assert snapshot['median_latency_ms'] == median
        assert snapshot['last_latency_ms'] == elapsed * 1000
    assert snapshot['failed_requests'] == 1
    snapshot['failed_requests'] = 99
    assert metrics.snapshot()['failed_requests'] == 1
    from rc_policy_sdk.monitor import normalize_snapshot
    assert normalize_snapshot(metrics.snapshot()) == metrics.snapshot()


def test_cancel_and_repeated_finish_do_not_add_samples(monkeypatch):
    module = metrics_module()
    now = [0.]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    metrics = module.RequestMetrics()
    metrics.finish(failed=True)
    metrics.start()
    metrics.cancel()
    metrics.finish(failed=True)
    assert metrics.snapshot() == EMPTY
    metrics.start()
    now[0] = .25
    metrics.finish(failed=True)
    now[0] = 10.
    metrics.finish(failed=True)
    assert metrics.snapshot() == dict(avg_latency_ms=250., median_latency_ms=250.,
                                      last_latency_ms=250., failed_requests=1)


def test_monitor_stays_responsive_in_flight_and_reset_retains_statistics(tmp_path, monkeypatch):
    module = metrics_module()
    now = [100.]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    with model_server() as server:
        model = make_backend(tmp_path, server.address)
        gate = threading.Event()
        try:
            model.load()
            assert model.monitor() == EMPTY  # Handshake isn't an observation request.
            with ThreadPoolExecutor(1) as pool:
                for index, duration in enumerate((.125, .375), 1):
                    gate.clear()
                    server.responses.put({'gate': gate})
                    pending = pool.submit(model.predict, np.zeros(14, np.float32), images(), 'task')
                    wait(lambda: len(server.requests) == index)
                    before = model.monitor()
                    for _ in range(10):
                        assert model.monitor() == before
                    assert not pending.done()
                    now[0] += duration
                    gate.set()
                    assert pending.result(2).shape == (50, 14)
            expected = dict(avg_latency_ms=250., median_latency_ms=250., last_latency_ms=375., failed_requests=0)
            assert model.monitor() == expected
            model.reset_context()
            assert model.monitor() == expected
            assert len(server.requests) == 2
        finally:
            gate.set()
            model.close()


@pytest.mark.parametrize('response', [
    {'body': {'type': 'stop', 'reason': 'operator'}},
    {'body': {'type': 'error', 'message': 'bad'}},
    {'body': {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute',
              'actions': {'joint_pos': np.zeros((49, 14), np.float32)}}},
    {'raw': b'\xc1'}, {'close': 1011}, {'gate': threading.Event()},
], ids=['stop', 'error', 'invalid-actions', 'bad-frame', 'disconnect', 'timeout'])
def test_failed_request_counted_once_before_disconnect_cleanup(tmp_path, monkeypatch, response):
    module = metrics_module()
    now = iter((100., 100.125))
    monkeypatch.setattr(module, 'monotonic', lambda: next(now))
    with model_server() as server:
        server.responses.put(response)
        model = make_backend(tmp_path, server.address, inference_timeout_s=.1)
        expected = dict(avg_latency_ms=125., median_latency_ms=125., last_latency_ms=125., failed_requests=1)
        disconnect = model._disconnect
        async def observe_disconnect():
            assert model.monitor() == expected
            await disconnect()
        try:
            model.load()
            monkeypatch.setattr(model, '_disconnect', observe_disconnect)
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert model.monitor() == expected
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert model.monitor() == expected and len(server.requests) == 1
        finally:
            monkeypatch.setattr(model, '_disconnect', disconnect)
            model.close()


def test_lifecycle_cancellation_is_not_a_failed_request(tmp_path):
    with model_server() as server:
        model = make_backend(tmp_path, server.address)
        server.responses.put({'gate': threading.Event()})
        try:
            model.load()
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(model.predict, np.zeros(14, np.float32), images(), 'task')
                wait(lambda: len(server.requests) == 1)
                model.abort()
                with pytest.raises(RuntimeError):
                    pending.result(2)
            assert model.monitor() == EMPTY
        finally:
            model.close()


def test_pre_send_validation_failure_has_no_request_statistics(tmp_path):
    with model_server() as server:
        model = make_backend(tmp_path, server.address)
        try:
            model.load()
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(13, np.float32), images(), 'task')
            assert model.monitor() == EMPTY and not server.requests
        finally:
            model.close()
