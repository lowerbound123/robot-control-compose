"""Local scheduling contract, with only the expensive model replaced."""
import importlib
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

PACKAGE = Path(__file__).resolve().parents[1] / 'packages/qwen35-policy/src'
sys.path.insert(0, str(PACKAGE))


def implementation():
    assert (PACKAGE / 'rc_qwen35_policy/runtime.py').exists(), 'local Qwen scheduler is missing'
    return importlib.import_module('rc_qwen35_policy.runtime').AsyncQwenPolicy


class Model:
    chunk_size = 50

    def __init__(self):
        self.calls = []
        self.events = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.output = np.zeros((50, 14), np.float32)
        self.error = None

    def load(self): self.events.append(('load', threading.get_ident()))
    def warmup(self): self.events.append(('warmup', threading.get_ident()))
    def reset_context(self): self.events.append(('reset', threading.get_ident()))
    def close(self): self.events.append(('close', threading.get_ident()))

    def predict(self, qpos, images, instruction):
        self.calls.append((qpos, images, instruction))
        self.events.append(('predict', threading.get_ident()))
        self.started.set()
        assert self.gate.wait(3), 'test inference gate timed out'
        if self.error:
            raise self.error
        return self.output.copy()


def metadata(count, epoch=1):
    return SimpleNamespace(consumed_actions=count, control_epoch=epoch)


def settings(timeout=2):
    return {'model': {'workflow': 'single'}, 'runtime': {'instruction': 'task'},
            'execution': {'inference_timeout_s': timeout}}


def images():
    return {name: np.zeros((2, 2, 3), np.uint8) for name in ('head', 'left_wrist', 'right_wrist')}


def step(policy, chunk, count, frames=None, epoch=1):
    return policy.step({'qpos': np.arange(14), 'timestamp': 1.0}, chunk,
                       images() if frames is None else frames, runtime=metadata(count, epoch))


@pytest.fixture
def running():
    model = Model()
    policy = implementation()(settings(), backend=model)
    try:
        yield policy, model
    finally:
        model.gate.set()
        policy.close()


def test_load_once_threshold_and_request_owned_snapshot(running):
    policy, model = running
    chunk = step(policy, [], 0)
    assert chunk.shape == (50, 14)
    # No request, preprocessing, or image read at 31 or 30 remaining actions.
    chunk = step(policy, chunk[19:], 19, {})
    chunk = step(policy, chunk[1:], 20, {})
    assert len(model.calls) == 1
    model.started.clear()
    model.gate.clear()
    frames = images()
    chunk = step(policy, chunk[1:], 21, frames)
    assert model.started.wait(1)
    frames['head'][:] = 99
    np.testing.assert_array_equal(model.calls[-1][1]['head'], 0)
    assert model.calls[-1][0].shape == (14,)
    chunk = step(policy, chunk[1:], 22, {})
    assert len(chunk) == 28 and len(model.calls) == 2
    assert [event for event, _ in model.events].count('load') == 1
    assert len({thread for _, thread in model.events}) == 1
    assert model.events[0][1] != threading.get_ident()


def test_consumed_prefix_and_full_overlap_fusion_process_grippers_once(running):
    policy, model = running
    model.output[:, [6, 13]] = -1.0
    chunk = step(policy, [], 0)
    assert chunk[0, 6] == pytest.approx(-0.85)
    model.output[:] = 10
    chunk = step(policy, chunk[21:], 21)
    policy.pending.result(1)
    # Seven complete targets consumed since the second observation.
    chunk = step(policy, chunk[7:], 28)
    assert chunk.shape == (43, 14)
    assert policy.last_trim == 7
    assert chunk[0, 0] == 0
    assert chunk[21, 0] == 10
    assert chunk[10, 0] == pytest.approx(6.2869202, abs=0.0001)
    np.testing.assert_allclose(chunk[:22, [6, 13]], -0.85)
    np.testing.assert_allclose(chunk[22:, [6, 13]], 10.15)
    unchanged = step(policy, chunk[1:], 29, {})
    np.testing.assert_array_equal(unchanged, chunk[1:])


def test_empty_queue_waits_for_existing_prediction_without_extra_request(running):
    policy, model = running
    chunk = step(policy, [], 0)
    model.started.clear()
    model.gate.clear()
    chunk = step(policy, chunk[21:], 21)
    assert model.started.wait(1)
    completed = threading.Event()
    result = []
    waiter = threading.Thread(target=lambda: (result.append(step(policy, [], 50)), completed.set()))
    waiter.start()
    assert not completed.wait(.03)
    # New short result may immediately schedule another refill; allow it.
    model.gate.set()
    waiter.join(2)
    assert completed.is_set()
    assert result[0].shape == (21, 14)
    assert policy.last_trim == 29


def test_reset_discards_pending_result_without_reloading_weights(running):
    policy, model = running
    chunk = step(policy, [], 0)
    step(policy, chunk[21:], 21)
    policy.pending.result(1)
    policy.reset()
    assert policy.pending is None and len(policy.canonical) == 0
    model.output[:] = 4
    chunk = step(policy, [], 21, epoch=2)
    assert chunk[0, 0] == 4
    assert [event for event, _ in model.events].count('load') == 1
    assert [event for event, _ in model.events].count('reset') == 1


@pytest.mark.parametrize('bad', [np.zeros((49, 14)), np.zeros((50, 16)), np.full((50, 14), np.nan)])
def test_model_must_produce_exact_finite_chunk(running, bad):
    policy, model = running
    model.output = bad
    with pytest.raises(ValueError): step(policy, [], 0)


def test_background_error_is_not_hidden_behind_cached_actions(running):
    policy, model = running
    chunk = step(policy, [], 0)
    model.error = RuntimeError('model failed')
    step(policy, chunk[21:], 21)
    with pytest.raises(RuntimeError, match='model failed'): policy.pending.result(1)
    with pytest.raises(RuntimeError, match='model failed'): step(policy, chunk[22:], 22)


def test_timeout_measured_from_submission_even_with_actions_remaining():
    model = Model()
    policy = implementation()(settings(.05), backend=model)
    try:
        chunk = step(policy, [], 0)
        model.started.clear()
        model.gate.clear()
        chunk = step(policy, chunk[21:], 21)
        assert model.started.wait(1)
        threading.Event().wait(.06)
        with pytest.raises(TimeoutError): step(policy, chunk, 21)
    finally:
        model.gate.set()
        policy.close()


def test_epoch_and_consumption_mismatch_rejected(running):
    policy, _ = running
    chunk = step(policy, [], 0)
    with pytest.raises(RuntimeError, match='epoch'): step(policy, chunk, 0, epoch=2)
    with pytest.raises(RuntimeError, match='remaining'): step(policy, chunk[:-1], 0)


def test_completed_prediction_can_be_collected_after_its_deadline():
    model = Model()
    policy = implementation()(settings(.05), backend=model)
    try:
        chunk = step(policy, [], 0)
        chunk = step(policy, chunk[21:], 21)
        policy.pending.result(1)
        threading.Event().wait(.06)
        result = step(policy, chunk, 21)
        assert result.shape == (50, 14)
        assert policy.last_trim == 0
    finally:
        policy.close()


def test_release_idempotent_and_stops_new_predictions(running):
    policy, model = running
    step(policy, [], 0)
    policy.close()
    policy.close()
    assert [event for event, _ in model.events].count('close') == 1
    with pytest.raises(RuntimeError): step(policy, [], 0)
