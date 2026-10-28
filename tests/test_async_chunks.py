"""The real shared scheduler; only GPU model inference is replaced."""
import importlib
import importlib.util
from types import SimpleNamespace
import threading

import numpy as np
import pytest


def scheduler():
    assert importlib.util.find_spec('rc_policy_sdk.async_chunks') is not None, 'shared chunk scheduler missing'
    return importlib.import_module('rc_policy_sdk.async_chunks').AsyncChunkPolicy


class Backend:
    def __init__(self, size=10):
        self.chunk_size = size
        self.output = np.zeros((size, 14), np.float32)
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.closed = 0
        self.calls = []

    def load(self): pass
    def warmup(self): pass
    def reset_context(self): pass
    def close(self): self.closed += 1

    def predict(self, qpos, images, instruction):
        self.calls.append((qpos, images, instruction))
        self.started.set()
        assert self.gate.wait(3), 'test inference was not released'
        return self.output.copy()


def step(policy, actions, consumed, epoch=1, frames=None):
    frames = frames if frames is not None else {
        key: np.zeros((2, 3, 3), np.uint8) for key in ('head', 'left_wrist', 'right_wrist')
    }
    return policy.step({'qpos': np.arange(14)}, actions, frames,
                       runtime=SimpleNamespace(consumed_actions=consumed, control_epoch=epoch))


def make(backend, **kwargs):
    return scheduler()(backend, instruction='task', chunk_size=10,
                       request_when_remaining=6, inference_timeout_s=2, **kwargs)


def test_configured_horizon_trims_consumed_prefix_and_preserves_grippers():
    backend = Backend()
    backend.output[:, [6, 13]] = -1
    policy = make(backend)
    try:
        chunk = step(policy, [], 0)
        np.testing.assert_equal(chunk[:, [6, 13]], -1)
        # Six remaining does not submit a second model request.
        chunk = step(policy, chunk[4:], 4, frames={})
        assert len(backend.calls) == 1
        backend.output[:] = 10
        backend.output[:, [6, 13]] = .4
        backend.started.clear()
        backend.gate.clear()
        chunk = step(policy, chunk[1:], 5)
        assert backend.started.wait(1)
        # Two complete targets consumed while inference is pending.
        chunk = step(policy, chunk[2:], 7, frames={})
        assert len(chunk) == 3
        backend.gate.set()
        policy.pending.result(1)
        chunk = step(policy, chunk, 7, frames={})
        assert chunk.shape == (8, 14) and policy.last_trim == 2
        assert chunk[0, 0] == 0 and chunk[2, 0] == 10
        np.testing.assert_equal(chunk[:3, [6, 13]], -1)
        np.testing.assert_allclose(chunk[3:, [6, 13]], .4)
    finally:
        backend.gate.set()
        policy.close()
    assert backend.closed == 1


def test_background_observation_is_owned_and_reset_drops_old_chunk():
    backend = Backend()
    policy = make(backend)
    try:
        chunk = step(policy, [], 0)
        frames = {key: np.zeros((2, 3, 3), np.uint8)
                  for key in ('head', 'left_wrist', 'right_wrist')}
        backend.gate.clear()
        backend.started.clear()
        step(policy, chunk[5:], 5, frames=frames)
        assert backend.started.wait(1)
        frames['head'][:] = 255
        np.testing.assert_equal(backend.calls[-1][1]['head'], 0)
        backend.gate.set()
        policy.reset()
        backend.output[:] = 4
        fresh = step(policy, [], 5, epoch=2)
        np.testing.assert_equal(fresh, 4)
        assert policy.pending is None and policy.last_trim == 0
    finally:
        backend.gate.set()
        policy.close()


@pytest.mark.parametrize('changes', [
    {'chunk_size': 0}, {'chunk_size': True}, {'request_when_remaining': 0},
    {'request_when_remaining': 11}, {'inference_timeout_s': float('nan')},
])
def test_invalid_schedule_fails_before_loading_model(changes):
    settings = dict(instruction='task', chunk_size=10, request_when_remaining=6,
                    inference_timeout_s=2)
    settings.update(changes)
    with pytest.raises(ValueError):
        scheduler()(Backend(), **settings)


def test_loaded_backend_horizon_mismatch_closes_model():
    backend = Backend(9)
    with pytest.raises(ValueError, match='chunk_size'):
        make(backend)
    assert backend.closed == 1
