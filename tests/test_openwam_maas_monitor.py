"""OpenWAM snapshots are readable while a request is pending on the wire."""
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from rc_policy_sdk.monitor import normalize_snapshot
from test_policy_monitor_worker import wait_until
from test_openwam_maas import SOURCES, peer, response, setup, step


@pytest.mark.parametrize('source', SOURCES)
def test_monitor_shows_transport_progress_and_last_valid_gripper_chunk(source):
    with peer() as server:
        module, _, ctx, _ = setup(source, server)
        def snapshot():
            return normalize_snapshot(module.monitor(ctx))
        try:
            initial = snapshot()
            assert initial['protocol_version'] == 2 and initial['service_origin'] == server.url
            assert initial['server_latency_ms'] == '—' and initial['last_request_bytes'] == 0
            assert initial['left_gripper_raw'] == initial['right_gripper_processed'] == '[]'

            reply = response()
            server.replies.put({**reply, 'latency_ms': 12.5})
            first = step(ctx, 0, [])
            _, request = server.messages.get(timeout=2)
            valid = snapshot()
            assert valid['server_latency_ms'] == 12.5
            assert valid['last_request_bytes'] == len(json.dumps(request, separators=(',', ':')).encode())
            for side, index in [('left', 6), ('right', 13)]:
                assert json.loads(valid[f'{side}_gripper_raw']) == [row[index] for row in reply['actions']]
                np.testing.assert_array_equal(json.loads(valid[f'{side}_gripper_processed']), first[:, index])
            for stage in ('encode', 'send', 'wait_response', 'decode', 'post'):
                assert 0 <= valid[stage + '_ms'] <= valid['predict_ms']

            server.replies.put('wait')
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(step, ctx, 32, [])
                try:
                    assert server.messages.get(timeout=2)[1]['type'] == 'chunk_obs'
                    wait_until(lambda: snapshot()['model_stage'] == 'wait_response', 1)
                    busy = snapshot()
                    assert busy['model_stage'] == 'wait_response'
                    assert busy['pending'] == busy['inference_busy'] == 1 and busy['inference_count'] == 2
                    assert busy['cached_actions'] == busy['request_when_remaining'] == 0
                    assert busy['server_latency_ms'] == 12.5 and busy['left_gripper_raw'] == valid['left_gripper_raw']
                    assert server.messages.empty()
                finally:
                    server.gate.set()
                pending.result(timeout=3)
            assert snapshot()['server_latency_ms'] == '—'  # Optional field absent in next reply.
            server.replies.put({**reply, 'actions': [[0]]})
            with pytest.raises(ValueError):
                step(ctx, 64, [])
            failed = snapshot()
            assert failed['last_error_stage'] == 'decode' and failed['inference_errors'] == 1
            assert failed['right_gripper_raw'] == str(np.asarray(response(100)['actions'])[:, 13].tolist())
            module.reset_context(ctx)
            clean = snapshot()
            assert clean['left_gripper_raw'] == clean['right_gripper_processed'] == '[]'
            assert clean['last_request_bytes'] == 0 and clean['server_latency_ms'] == '—'
            assert clean['inference_completed'] == 2
        finally:
            module.release(ctx)


@pytest.mark.parametrize('source', SOURCES)
def test_invalid_optional_latency_keeps_monitor_and_actions_available(source):
    with peer() as server:
        module, _, ctx, _ = setup(source, server)
        try:
            for index, latency in enumerate([False, -1, 'slow', float('nan'), 0]):
                server.replies.put({**response(), 'latency_ms': latency})
                assert step(ctx, 32 * index, []).shape == (32, 14)
                values = normalize_snapshot(module.monitor(ctx))
                assert values['server_latency_ms'] == (0 if index == 4 else '—')
                assert values['inference_errors'] == 0
        finally:
            module.release(ctx)
