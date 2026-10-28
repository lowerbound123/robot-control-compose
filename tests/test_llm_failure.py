import base64
import copy
import io
import threading
import time

import numpy as np
from PIL import Image
import pytest

from llm_failure_helpers import Provider, Runtime, done, images, make, manifest, wait


@pytest.mark.parametrize('mode,value', [
    ('qpos', [[.1] * 14] * 3),
    ('epos', np.ones((3, 14), np.float64)),
    ('mixed', {'actions': [[.1] * 14, [.2] * 14], 'modes': ['qpos', 'epos']}),
])
def test_passthrough_identity_and_actual_step_boundaries(mode, value):
    provider, runtime = Provider(), Runtime(300)
    config = manifest()
    config['execution']['control_mode'] = mode
    instance = make(config, provider)
    original = copy.deepcopy(value)
    try:
        for count in (300, 301, 359):
            runtime.consumed_actions = count
            assert instance.policy(value, images() if count == 300 else {}, runtime) is value
        assert provider.calls == []
        runtime.consumed_actions = 360
        assert instance.policy(value, images(30), runtime) is value
        done(instance)
        assert [sample.step for sample in provider.calls[0][2]] == [0, 60]
        for count in (360, 360, 419):
            runtime.consumed_actions = count
            assert instance.policy(value, {}, runtime) is value
        assert len(provider.calls) == 1
        runtime.consumed_actions = 420
        assert instance.policy(value, images(40), runtime) is value
        done(instance)
        assert [sample.step for sample in provider.calls[1][2]] == [0, 60, 120]
        assert runtime.finishes == []
        if isinstance(value, np.ndarray):
            np.testing.assert_array_equal(value, original)
        else:
            assert value == original
    finally:
        instance.close()


def test_busy_request_never_blocks_or_queues_and_uses_latest_snapshot():
    gate = threading.Event()
    provider, runtime = Provider((gate, False)), Runtime()
    instance = make(provider=provider)
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(30), runtime)
        wait(lambda: len(provider.calls) == 1)
        started = time.monotonic()
        for count in (120, 180, 240):
            runtime.consumed_actions = count
            assert instance.policy(chunk, {}, runtime) is chunk
        assert time.monotonic() - started < .5
        assert len(provider.calls) == 1
        gate.set()
        done(instance)
        runtime.consumed_actions = 245
        instance.policy(chunk, images(90), runtime)
        done(instance)
        assert len(provider.calls) == 2
        assert [sample.step for sample in provider.calls[-1][2]] == [0, 60, 245]
        runtime.consumed_actions = 304
        instance.policy(chunk, {}, runtime)
        assert len(provider.calls) == 2
    finally:
        gate.set()
        instance.close()


def test_failed_verdict_calls_finish_only_on_next_callback_once():
    provider, runtime = Provider(True), Runtime()
    instance = make(provider=provider)
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        assert runtime.finishes == []
        runtime.consumed_actions = 61
        assert instance.policy(chunk, {}, runtime) is chunk
        assert runtime.finishes == [('llm_failure：DeepSeek 判断任务已进入失败状态', 'terminated')]
        runtime.consumed_actions = 300
        assert instance.policy(chunk, {}, runtime) is chunk
        assert len(provider.calls) == len(runtime.finishes) == 1
    finally:
        instance.close()


def test_history_is_bounded_ordered_and_owns_pixels_after_callback():
    provider, runtime = Provider(), Runtime()
    instance = make(provider=provider)
    chunk, initial = [[0.] * 14], images(10)
    try:
        instance.policy(chunk, initial, runtime)
        for frame in initial.values():
            frame.fill(255)
        for step in range(60, 361, 60):
            runtime.consumed_actions = step
            instance.policy(chunk, images(20 + step // 6), runtime)
            done(instance)
            instance.policy(chunk, {}, runtime)
        first = provider.calls[0][2][0]
        encoded = dict(first.images)['head']
        with Image.open(io.BytesIO(base64.b64decode(encoded.split(',')[1]))) as decoded:
            assert np.asarray(decoded).max() <= 11
        latest = provider.calls[-1][2]
        assert [sample.step for sample in latest] == [120, 180, 240, 300, 360]
        assert all(tuple(name for name, _ in sample.images) == ('head', 'left_wrist', 'right_wrist') for sample in latest)
        assert len(provider.calls[0][2]) == 2, 'later samples must not mutate an in-flight snapshot'
    finally:
        instance.close()


@pytest.mark.parametrize('reset', ['explicit', 'epoch', 'counter'])
def test_new_context_discards_old_results_and_restarts_step_count(reset):
    provider, runtime = Provider(True), Runtime(100)
    instance = make(provider=provider)
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 160
        instance.policy(chunk, images(), runtime)
        done(instance)
        if reset == 'explicit':
            instance.reset_context()
        elif reset == 'epoch':
            runtime.control_epoch += 1
        else:
            runtime.consumed_actions = 10
        baseline = runtime.consumed_actions
        instance.policy(chunk, images(80), runtime)
        assert runtime.finishes == []
        runtime.consumed_actions = baseline + 59
        instance.policy(chunk, {}, runtime)
        assert len(provider.calls) == 1
        runtime.consumed_actions += 1
        instance.policy(chunk, images(90), runtime)
        done(instance)
        assert [sample.step for sample in provider.calls[-1][2]] == [0, 60]
    finally:
        instance.close()


def test_reset_waits_for_old_request_without_applying_its_error():
    gate = threading.Event()
    provider, runtime = Provider((gate, RuntimeError('stale request'))), Runtime()
    instance = make(provider=provider)
    chunk = [[0.] * 14]
    thread = None
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        wait(lambda: provider.calls)
        reset_done = threading.Event()
        def reset():
            instance.reset_context()
            reset_done.set()
        thread = threading.Thread(target=reset)
        thread.start()
        assert not reset_done.wait(.05)
        gate.set()
        assert reset_done.wait(2)
        assert instance.policy(chunk, images(), runtime) is chunk
        assert runtime.finishes == []
    finally:
        gate.set()
        if thread:
            thread.join(2)
        instance.close()
        instance.close()
    assert provider.closed == 1


def test_background_error_and_deadline_are_reported_in_policy_thread(monkeypatch):
    from rc_llm_failure.errors import ProviderError
    import rc_llm_failure.runtime as module
    provider, runtime = Provider(ProviderError('API HTTP 503')), Runtime()
    instance = make(provider=provider)
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        with pytest.raises(ProviderError, match='503'):
            instance.policy(chunk, {}, runtime)
        assert runtime.finishes == []
    finally:
        instance.close()
    gate = threading.Event()
    instance = make(provider=Provider((gate, False)))
    now = [100.]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    try:
        runtime = Runtime()
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        now[0] = 104.
        with pytest.raises(ProviderError, match='超时'):
            instance.policy(chunk, {}, runtime)
        assert runtime.finishes == []
    finally:
        gate.set()
        instance.close()


@pytest.mark.parametrize('patch', [
    {'task': ''}, {'prompt': ''}, {'check_every_steps': True}, {'check_every_steps': 0},
    {'history_size': 1}, {'history_size': 17}, {'image_max_side': 0}, {'jpeg_quality': 96}, {'unknown': 1},
])
def test_bad_configuration_fails_before_initialization(patch):
    with pytest.raises(ValueError):
        make(manifest(**patch))


def test_invalid_actions_or_images_never_start_network():
    provider, runtime = Provider(), Runtime()
    instance = make(provider=provider)
    try:
        with pytest.raises(ValueError):
            instance.policy([], images(), runtime)
        with pytest.raises(ValueError):
            instance.policy([[float('nan')] * 14], images(), runtime)
        with pytest.raises(ValueError):
            instance.policy([[0.] * 14], {}, runtime)
        assert provider.calls == []
    finally:
        instance.close()
