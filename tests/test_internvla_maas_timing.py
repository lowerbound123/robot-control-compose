"""Known-clock phase measurements on the production MaaS request path."""
import importlib
import sys
import threading

import numpy as np
import pytest

from internvla_maas_helpers import configured, images, model_server, settings, wait

pytest.importorskip('msgpack')
pytest.importorskip('websockets')
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason='MaaS image uses Python 3.11')


def test_stage_averages_last_values_counts_and_partial_work(monkeypatch):
    module = importlib.import_module('rc_internvla_maas.metrics')
    now = [100.]
    monkeypatch.setattr(module, 'perf_counter', lambda: now[0], raising=False)
    metrics = module.StageTimings(('pack', 'server'), last_request_bytes='—')
    assert metrics.snapshot()['last_pack_ms'] == '—'
    assert metrics.snapshot()['pack_samples'] == 0
    with metrics.measure('pack'):
        now[0] += .125
    with pytest.raises(ValueError):
        with metrics.measure('pack'):
            now[0] += .375
            raise ValueError('partial work still took time')
    metrics.record('server', 1.25)
    metrics.missing('server')
    metrics.set_values(last_request_bytes=4096)
    snapshot = metrics.snapshot()
    assert snapshot == {'avg_pack_ms': 250., 'last_pack_ms': 375., 'pack_samples': 2,
                        'avg_server_ms': 1.25, 'last_server_ms': '—', 'server_samples': 1,
                        'last_request_bytes': 4096}
    snapshot['pack_samples'] = 99
    assert metrics.snapshot()['pack_samples'] == 2
    for invalid in (float('nan'), float('inf'), -1):
        metrics.record('server', invalid)
    assert metrics.snapshot()['server_samples'] == 1


def test_request_phases_and_byte_sizes_measure_actual_operations(tmp_path, monkeypatch):
    from rc_internvla_maas import backend, metrics
    with model_server() as server:
        model = backend.MaasBackend(configured(tmp_path, settings(server.address)))
        now, sent, received = [100.], [], []
        try:
            model.load()
            initial = model.monitor()
            assert initial['send_samples'] == initial['decode_samples'] == 0
            monkeypatch.setattr(metrics, 'perf_counter', lambda: now[0], raising=False)
            original_pack, original_unpack = backend.pack, backend.unpack
            original_resize = backend.resize_rgb
            original_send, original_recv = model._ws.send, model._ws.recv
            def pack(value):
                result = original_pack(value)
                now[0] += .002
                return result
            def resize(value):
                result = original_resize(value)
                now[0] += .001
                return result
            async def send(frame):
                sent.append(len(frame))
                await original_send(frame)
                now[0] += .004
            async def recv():
                frame = await original_recv()
                received.append(len(frame))
                now[0] += .05
                return frame
            def unpack(frame):
                result = original_unpack(frame)
                now[0] += .003
                return result
            monkeypatch.setattr(backend, 'pack', pack)
            monkeypatch.setattr(backend, 'resize_rgb', resize)
            monkeypatch.setattr(backend, 'unpack', unpack)
            monkeypatch.setattr(model._ws, 'send', send)
            monkeypatch.setattr(model._ws, 'recv', recv)
            result = model.predict(np.zeros(14, np.float32), images(), 'task')
            assert result.shape == (50, 14)
            snapshot = model.monitor()
            for name, expected in (('resize', 3.), ('prepare', 3.), ('pack', 2.), ('send', 4.), ('response_wait', 50.), ('decode', 3.), ('server', 1.25)):
                assert snapshot[f'last_{name}_ms'] == pytest.approx(expected)
                assert snapshot[f'avg_{name}_ms'] == pytest.approx(expected)
                assert snapshot[f'{name}_samples'] == 1
            assert snapshot['dispatch_samples'] == snapshot['prepare_samples'] == snapshot['validate_samples'] == 1
            assert snapshot['last_source_image_bytes'] == 54
            assert snapshot['last_image_bytes'] == 451584
            assert snapshot['last_request_bytes'] == sent[0]
            assert snapshot['last_response_bytes'] == received[0]
            assert snapshot['failed_requests'] == 0
            assert model.monitor() == snapshot and len(server.requests) == 1
        finally:
            model.close()


@pytest.mark.parametrize('value', [None, -1, float('nan'), float('inf'), True, '4.5'])
def test_missing_or_invalid_server_time_is_not_inferred_or_an_action_fault(tmp_path, value):
    from rc_internvla_maas.backend import MaasBackend
    with model_server() as server:
        model = MaasBackend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            model.predict(np.zeros(14, np.float32), images(), 'task')
            body = {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute',
                    'actions': {'joint_pos': np.ones((50, 14), np.float32)}}
            if value is not None:
                body['server_ms'] = value
            server.responses.put({'body': body})
            model.predict(np.zeros(14, np.float32), images(), 'task')
            snapshot = model.monitor()
            assert snapshot['last_server_ms'] == '—'
            assert snapshot['avg_server_ms'] == 1.25 and snapshot['server_samples'] == 1
            assert snapshot['failed_requests'] == 0
        finally:
            model.close()


def test_failed_decode_does_not_invent_validation_or_server_samples(tmp_path):
    from rc_internvla_maas.backend import MaasBackend
    with model_server() as server:
        model = MaasBackend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            server.responses.put({'raw': b'\xc1'})
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            snapshot = model.monitor()
            assert snapshot['failed_requests'] == 1
            assert snapshot['response_wait_samples'] == snapshot['decode_samples'] == 1
            assert snapshot['validate_samples'] == snapshot['server_samples'] == 0
            assert snapshot['last_validate_ms'] == snapshot['last_server_ms'] == '—'
            assert snapshot['last_response_bytes'] == 1
        finally:
            model.close()


def test_runtime_reports_collection_wait_without_changing_scheduler(tmp_path, monkeypatch):
    from rc_internvla_maas import runtime as module
    from rc_policy_sdk.monitor import normalize_snapshot
    from types import SimpleNamespace
    with model_server() as server:
        context = module.AsyncMaasPolicy(configured(tmp_path, settings(server.address)))
        gate = threading.Event()
        def step(chunk, count, frames):
            return context.step({'qpos': np.zeros(14, np.float32)}, chunk, frames,
                                runtime=SimpleNamespace(consumed_actions=count, control_epoch=1))
        try:
            chunk = step([], 0, images())
            chunk = step(chunk[1:], 1, {})
            cached = context.monitor()
            assert cached['callback_samples'] == 2
            for name in ('snapshot_submit', 'predict', 'collect', 'postprocess', 'result_ready_wait'):
                assert cached[f'{name}_samples'] == 1
            server.responses.put({'gate': gate})
            chunk = step(chunk[20:], 21, images())
            wait(lambda: len(server.requests) == 2)
            waiting = context.monitor()
            assert waiting['snapshot_submit_samples'] == 2 and waiting['predict_samples'] == 1
            assert context.monitor() == waiting
            gate.set()
            _, completed = context.pending.result(2)
            monkeypatch.setattr(module, 'monotonic', lambda: completed + .125, raising=False)
            chunk = step(chunk[7:], 28, {})
            snapshot = context.monitor()
            assert chunk.shape == (43, 14) and context.last_trim == 7
            assert snapshot['last_result_ready_wait_ms'] == pytest.approx(125.)
            assert snapshot['callback_samples'] == 4
            assert snapshot['collect_samples'] == snapshot['predict_samples'] == snapshot['postprocess_samples'] == 2
            assert normalize_snapshot(snapshot) == snapshot
            context.reset()
            assert context.monitor() == snapshot
        finally:
            gate.set()
            context.close()
