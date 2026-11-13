"""Consumed-target cadence and owned history, with only the GPU boundary replaced."""
import importlib
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'policy/qwen35_history/src'))


def implementation():
    assert (ROOT / 'policy/qwen35_history/src/rc_qwen35_history/runtime.py').is_file(), 'history policy runtime missing'
    return importlib.import_module('rc_qwen35_history.runtime').HistoryPolicy


class Backend:
    chunk_size = 50
    history_slot_count = 32
    history_slot_stride = 15

    def __init__(self):
        self.calls = []
        self.events = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.output = np.zeros((50, 14), np.float32)
        self.output[:, [6, 13]] = -1
        self.error = None

    def load(self): self.events.append(('load', threading.get_ident()))
    def warmup(self): self.events.append(('warmup', threading.get_ident()))
    def reset_context(self): self.events.append(('reset', threading.get_ident()))
    def close(self): self.events.append(('close', threading.get_ident()))
    def resize_history(self, image): return image.copy()

    def predict(self, qpos, images, instruction, contexts):
        self.calls.append((qpos, images, instruction, contexts))
        self.events.append(('predict', threading.get_ident()))
        self.started.set()
        assert self.gate.wait(3), 'test gate timed out'
        if self.error:
            raise self.error
        return self.output.copy(), {'prefix_ms': 1.0, 'denoise_ms': 2.0}


def config(x=25, pipelined=False, timeout=2):
    return {'runtime': {'instruction': 'untangle'},
            'execution': {'replan_every_actions': x, 'pipelined': pipelined, 'inference_timeout_s': timeout},
            'history': {'enabled': True}}


def frames(value=0):
    return {name: np.full((2, 3, 3), value, np.uint8) for name in ('head', 'left_wrist', 'right_wrist')}


def step(policy, remaining, count, *, image=None, epoch=1):
    return policy.step({'qpos': np.arange(14, dtype=np.float32)}, remaining,
                       frames(count % 256) if image is None else image,
                       runtime=SimpleNamespace(consumed_actions=count, control_epoch=epoch))


@pytest.fixture
def make():
    policies = []
    def create(x=25, pipelined=False, timeout=2, manifest=None):
        backend = Backend()
        policy = implementation()(config(x, pipelined, timeout), backend=backend, policy_config=manifest)
        policies.append((policy, backend))
        return policy, backend
    yield create
    for policy, backend in policies:
        backend.gate.set()
        policy.close()


@pytest.mark.parametrize('x', [1, 25, 50])
def test_remaining_threshold_replans_and_returns_full_prediction(make, x):
    policy, backend = make(x)
    chunk = step(policy, [], 0)
    assert chunk.shape == (50, 14)
    for count in range(1, 2*x+1):
        chunk = step(policy, chunk[1:], count)
        assert len(backend.calls) == 1 + count // x
    assert [f.index for f in backend.calls[-1][3]] == list(range(2*x % 15, 2*x-14, 15))
    assert policy.monitor()['history_tick'] == 2*x
    assert len({ident for _, ident in backend.events}) == 1
    assert backend.events[0][1] != threading.get_ident()


def test_history_indices_capacity_ownership_and_duplicate_counter(make):
    policy, backend = make(1)
    chunk = step(policy, [], 0)
    for count in range(1, 501):
        image = frames(count % 256)
        chunk = step(policy, chunk[1:], count, image=image)
        image['head'][:] = 255
        if count in (14, 15, 25, 480):
            expected = {14: [], 15: [0], 25: [10], 480: list(range(0, 480, 15))}[count]
            assert [f.index for f in backend.calls[-1][3]] == expected
    assert policy.monitor()['history_frames'] == 481
    saved = backend.calls[-1][3][0]
    assert np.all(saved.image == (saved.index+1) % 256)
    assert not saved.image.flags.writeable
    calls = len(backend.calls)
    step(policy, chunk, 500)
    assert len(backend.calls) == calls
    assert policy.monitor()['history_tick'] == 500


@pytest.mark.parametrize('history_enabled', [False, True])
def test_counter_jump_fills_current_observation_and_consumes_exact_tail(make, caplog, history_enabled):
    policy, backend = make()
    policy.history_enabled = history_enabled
    backend.output[:, 0] = np.arange(50)
    chunk = step(policy, [], 100)
    chunk = step(policy, chunk[1:], 101, image=frames(11))
    image = frames(77)
    chunk = step(policy, chunk[3:], 104, image=image)
    image['head'][:] = 255
    np.testing.assert_array_equal(chunk[:, 0], np.arange(4, 50))
    assert len(backend.calls) == 1
    if history_enabled:
        assert policy.monitor()['history_tick'] == 4
        assert [f.index for f in policy.history.frames] == [0, 1, 2, 3]
        assert [int(f.image[0, 0, 0]) for f in policy.history.frames] == [11, 77, 77, 77]
        assert all(not f.image.flags.writeable for f in policy.history.frames)
        assert any(record.levelname == 'WARNING' and '101' in record.message and '104' in record.message
                   for record in caplog.records)
    else:
        assert policy.monitor()['history_tick'] == 0 and not policy.history.frames
    step(policy, chunk, 104, image=frames(88))
    assert policy.monitor()['history_tick'] == (4 if history_enabled else 0)
    step(policy, chunk[1:], 105, image=frames(99))
    assert policy.monitor()['history_tick'] == (5 if history_enabled else 0)
    if history_enabled:
        assert int(policy.history.frames[-1].image[0, 0, 0]) == 99


def test_counter_jumps_replan_with_filled_history_and_keep_capacity(make):
    policy, backend = make(25)
    chunk = step(policy, [], 0)
    for count in range(25, 501, 25):
        chunk = step(policy, chunk[25:], count)
        assert len(chunk) == 50
        assert len(backend.calls) == 1 + count // 25
    assert policy.monitor()['history_tick'] == 500
    assert policy.monitor()['history_frames'] == 481
    assert policy.history.frames[0].index == 19 and policy.history.frames[-1].index == 499
    contexts = backend.calls[-1][3]
    assert [f.index for f in contexts] == list(range(20, 500, 15))
    assert int(contexts[0].image[0, 0, 0]) == 25
    assert int(contexts[1].image[0, 0, 0]) == 50
    assert int(contexts[-1].image[0, 0, 0]) == 500 % 256
    policy.reset()
    step(policy, [], 0, epoch=2)
    assert policy.monitor()['history_tick'] == 0 and not policy.history.frames
    assert backend.calls[-1][3] == ()


@pytest.mark.parametrize('count,epoch', [(-1, 1), (1, 0)])
def test_backward_counters_and_epochs_remain_errors(make, count, epoch):
    policy, backend = make()
    chunk = step(policy, [], 0)
    with pytest.raises(RuntimeError, match='backwards'):
        step(policy, chunk, count, epoch=epoch)


def test_task_change_and_reset_discard_old_work_and_clear_history(make):
    manifest = {'params': {'task': None}}
    policy, backend = make(15, True, manifest=manifest)
    chunk = step(policy, [], 0)
    for count in range(1, 17):
        chunk = step(policy, chunk[1:], count)
    manifest['params']['task'] = 'new task'
    chunk = step(policy, chunk, 16)
    assert backend.calls[-1][2:] == ('new task', ())
    assert policy.monitor()['history_tick'] == 0
    assert sum(name == 'load' for name, _ in backend.events) == 1
    policy.reset()
    step(policy, [], 0, epoch=2)
    assert backend.calls[-1][3] == ()


def test_timeout_and_model_error_remain_faulted_until_cold_reload(make):
    policy, backend = make(timeout=.01)
    backend.gate.clear()
    with pytest.raises(TimeoutError):
        step(policy, [], 0)
    backend.gate.set()
    with pytest.raises(TimeoutError):
        policy.reset()
    with pytest.raises(TimeoutError):
        step(policy, [], 0)


def test_instances_do_not_share_frames_or_inference(make):
    one, a = make(1)
    two, b = make(1)
    ca, cb = step(one, [], 0), step(two, [], 0)
    for count in range(1, 16):
        ca = step(one, ca[1:], count, image=frames(11))
        cb = step(two, cb[1:], count, image=frames(22))
    assert np.all(a.calls[-1][3][0].image == 11)
    assert np.all(b.calls[-1][3][0].image == 22)


@pytest.mark.parametrize('bad', ['shape','nan','exception'])
def test_invalid_predictions_fault_without_installing_actions(make, bad):
    policy, backend = make()
    if bad == 'shape': backend.output = np.zeros((49,14),np.float32)
    if bad == 'nan': backend.output[0,0] = np.nan
    if bad == 'exception': backend.error = ValueError('model exploded')
    with pytest.raises(ValueError): step(policy,[],0)
    assert len(policy.canonical) == 0
    with pytest.raises(ValueError): step(policy,[],0)


def test_empty_queue_blocks_this_callback_until_full_prediction_is_ready(make):
    from concurrent.futures import ThreadPoolExecutor
    policy, backend = make(25, True)
    backend.gate.clear()
    with ThreadPoolExecutor(1) as executor:
        waiting = executor.submit(step, policy, [], 50)
        try:
            assert backend.started.wait(1)
            assert not waiting.done() and len(backend.calls) == 1
        finally:
            backend.gate.set()
        result = waiting.result(2)
    assert len(result) == 50 and policy.monitor()['last_trim'] == 0
    policy.reset()
    result = step(policy, [], 0, epoch=2)
    assert len(result) == 50 and backend.calls[-1][3] == ()


def test_monitor_exposes_only_sdk_scalar_types(make):
    from rc_policy_sdk.monitor import normalize_snapshot
    policy, backend = make(1)
    step(policy,[],0)
    snapshot = normalize_snapshot(policy.monitor())
    assert snapshot['inference_count']==1 and snapshot['cached_actions']==50
    assert snapshot['prefix_ms']==1. and snapshot['denoise_ms']==2.
