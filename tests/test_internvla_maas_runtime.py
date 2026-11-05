import importlib
import json
import threading
import time
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from internvla_maas_helpers import PACKAGE, configured, images, model_server, settings, wait

pytest.importorskip('websockets')
pytest.importorskip('msgpack')
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason='MaaS policy image uses Python 3.11')


def policy(config, root):
    assert (PACKAGE / 'rc_internvla_maas/runtime.py').is_file(), 'MaaS scheduling adapter missing'
    return importlib.import_module('rc_internvla_maas.runtime').AsyncMaasPolicy(config, project_root=root)


def step(context, chunk, count, *, epoch=1, frames=None):
    qpos = np.zeros(14, np.float32)
    qpos[[6, 13]] = -1
    return context.step({'qpos': qpos}, chunk, images() if frames is None else frames,
                        runtime=SimpleNamespace(consumed_actions=count, control_epoch=epoch))


def test_prefetch_trim_blend_and_apply_gripper_compensation_only_once(tmp_path):
    with model_server() as server:
        context = policy(configured(tmp_path, settings(server.address)), tmp_path)
        try:
            old = step(context, [], 0)
            np.testing.assert_allclose(old[:, [6, 13]], -.85)
            old = step(context, old[20:], 20, frames={})
            assert len(server.requests) == 1  # Exactly 30 remaining is not a refill.
            gate = threading.Event()
            server.responses.put({'gate': gate, 'body': {'type': 'actions', 'space': 'joint_pos',
                'mode': 'absolute', 'actions': {'joint_pos': np.full((50, 14), 10, np.float32)}}})
            frames = images()
            old = step(context, old[1:], 21, frames=frames)
            frames['head'][:] = 99
            wait(lambda: len(server.requests) == 2)
            assert server.requests[-1]['images']['front'][0, 0, 0] == 1
            old = step(context, old[1:], 22, frames={})
            assert len(server.requests) == 2
            gate.set()
            context.pending.result(2)
            actual = step(context, old[6:], 28, frames={})
            assert actual.shape == (43, 14) and context.last_trim == 7
            assert actual[0, 0] == 0 and actual[21, 0] == 10
            assert actual[10, 0] == pytest.approx(6.2869202, abs=.0001)
            np.testing.assert_allclose(actual[:22, [6, 13]], -.85)
            np.testing.assert_allclose(actual[22:, [6, 13]], 10.15)
            np.testing.assert_array_equal(step(context, actual[1:], 29, frames={}), actual[1:])
        finally:
            context.close()


def test_disabled_compensation_and_threshold_boundary(tmp_path):
    for enabled, expected in [(False, -1), (True, -.85)]:
        with model_server() as server:
            raw = settings(server.address)
            raw['actions'] = {'gripper_compensation': enabled}
            context = policy(configured(tmp_path, raw), tmp_path)
            try:
                actual = step(context, [], 0)
                np.testing.assert_allclose(actual[:, [6, 13]], expected)
                values = np.zeros((1, 14), np.float32)
                values[0, [6, 13]] = [-2.0, -2.1]
                np.testing.assert_array_equal(context._process(values), values)
            finally:
                context.close()


def test_reset_and_release_cancel_inflight_instead_of_waiting_sixty_seconds(tmp_path):
    with model_server() as server:
        context = policy(configured(tmp_path, settings(server.address)), tmp_path)
        try:
            chunk = step(context, [], 0)
            original_episode = server.requests[0]['episode_id']
            server.responses.put({'gate': threading.Event()})
            step(context, chunk[21:], 21)
            wait(lambda: len(server.requests) == 2)
            started = time.monotonic()
            context.reset()
            assert time.monotonic() - started < 2
            assert context.pending is None and len(context.canonical) == 0
            chunk = step(context, [], 21, epoch=2)
            assert server.requests[-1]['episode_id'] != original_episode
            server.responses.put({'gate': threading.Event()})
            step(context, chunk[21:], 42, epoch=2)
            wait(lambda: len(server.requests) == 4)
            started = time.monotonic()
            context.close()
            assert time.monotonic() - started < 2
            context.close()
        finally:
            context.close()


def test_failed_background_request_stops_cached_actions_and_reset(tmp_path):
    with model_server() as server:
        context = policy(configured(tmp_path, settings(server.address)), tmp_path)
        try:
            chunk = step(context, [], 0)
            server.responses.put({'body': {'type': 'stop', 'reason': 'operator'}})
            step(context, chunk[21:], 21)
            with pytest.raises(RuntimeError):
                context.pending.result(2)
            with pytest.raises(RuntimeError):
                step(context, chunk[22:], 22)
            with pytest.raises(RuntimeError):
                context.reset()
            assert len(server.hellos) == 1 and len(server.requests) == 2
        finally:
            context.close()


def test_switch_model_only_validates_and_returns_cold_patch(tmp_path):
    with model_server() as server:
        context = policy(configured(tmp_path, settings(server.address)), tmp_path)
        try:
            alternate = settings('ws://unreachable.example')
            target = tmp_path / 'alternate.json'
            target.write_text(json.dumps(alternate))
            assert context.switch_model('alternate.json') == {'params.model_config_path': str(target)}
            assert len(server.hellos) == 1 and not server.requests
            alternate['execution'] = {'chunk_size': 49}
            target.write_text(json.dumps(alternate))
            with pytest.raises(ValueError):
                context.switch_model('alternate.json')
            step(context, [], 0)
        finally:
            context.close()
