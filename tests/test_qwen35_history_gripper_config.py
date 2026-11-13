"""Live threshold changes preserve both history instances and raw action plans."""
import copy
import importlib.util
from types import SimpleNamespace

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import ROOT
from test_qwen35_history_runtime import Backend, config, frames


@pytest.fixture(params=['qwen35_history', 'qwen35_history_lift2'])
def entry(request):
    source = ROOT / 'policy' / request.param
    spec = importlib.util.spec_from_file_location('threshold_' + request.param, source / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, read_config(source / 'policy.yaml.example'), request.param


@pytest.mark.parametrize('pipelined', [False, True])
def test_threshold_commit_changes_cached_grippers_without_reset_or_inference(entry, pipelined):
    module, active, source = entry
    lift2 = source.endswith('lift2')
    initial, updated = (2.5, 3.5) if lift2 else (-.8, -1.2)
    assert active['params']['gripper_threshold'] == initial
    assert 'params.gripper_threshold' in active['hot_reloadable']
    backend = Backend()
    backend.output[:, [6, 13]] = [3., 3.5] if lift2 else [-1., -1.2]
    raw = backend.output.copy()
    context = module.HistoryPolicy(config(25, pipelined), policy_config=active, backend=backend)
    try:
        state = {'qpos': np.zeros(14, np.float32)}
        output = context.step(state, [], frames(), runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        np.testing.assert_allclose(output[:, [6, 13]], np.tile([4.9, 4.9] if lift2 else [-1., -1.2], (len(output), 1)))
        candidate = copy.deepcopy(active)
        effects = []
        module.configure(context, candidate, 'params.gripper_threshold', str(updated), effects.append)
        assert effects == [] and active['params']['gripper_threshold'] == initial
        assert candidate['params']['gripper_threshold'] == updated
        # Normal SDK commits update the existing dict; the context retains that reference.
        active['params'].update(candidate['params'])
        output = context.step(state, [], frames(1),
                              runtime=SimpleNamespace(consumed_actions=1, control_epoch=2, config_version=2))
        np.testing.assert_allclose(output[:, [6, 13]], np.tile([0., 0.] if lift2 else [.04, -1.2], (len(output), 1)))
        np.testing.assert_array_equal(context.canonical, raw[:len(output)])
        np.testing.assert_array_equal(output[:, :6], raw[:len(output), :6])
        assert context.policy_config is active and context.history.tick == 1
        assert len(backend.calls) == 2
        assert [name for name, _ in backend.events] == ['load', 'warmup', 'predict', 'predict']
    finally:
        context.close()


@pytest.mark.parametrize('raw', ['true', 'null', '"1.2"', 'NaN', 'Infinity', '-Infinity', '[]'])
def test_invalid_threshold_rejects_candidate_without_lifecycle_effect(entry, raw):
    module, active, _ = entry
    candidate = copy.deepcopy(active)
    effects = []
    with pytest.raises(ValueError, match='gripper_threshold'):
        module.configure(None, candidate, 'params.gripper_threshold', raw, effects.append)
    assert candidate == active and effects == []


@pytest.mark.parametrize('value', [True, None, '1.2', float('nan'), float('inf'), []])
def test_manifest_rejects_invalid_threshold_before_initialization(entry, value):
    module, active, _ = entry
    active['params']['gripper_threshold'] = value
    with pytest.raises(ValueError, match='gripper_threshold'):
        module.validate_config(active)


def test_legacy_manifest_without_threshold_keeps_existing_default(entry):
    module, active, source = entry
    active['params'].pop('gripper_threshold', None)
    module.validate_config(active)
    backend = Backend()
    backend.output[:, [6, 13]] = [2.5, 3.] if source.endswith('lift2') else [-.8, -.7]
    context = module.HistoryPolicy(config(), policy_config=active, backend=backend)
    try:
        output = context.step({'qpos': np.zeros(14, np.float32)}, [], frames(),
                              runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        expected = [0., 4.9] if source.endswith('lift2') else [-.8, .04]
        np.testing.assert_allclose(output[:, [6, 13]], np.tile(expected, (len(output), 1)))
    finally:
        context.close()


@pytest.mark.parametrize('pipelined', [False, True])
def test_committed_config_epoch_keeps_history_and_replans_from_fresh_observation(entry, pipelined):
    module, active, source = entry
    backend = Backend()
    context = module.HistoryPolicy(config(1 if pipelined else 25, pipelined),
                                   policy_config=active, backend=backend)
    try:
        old_state = {'qpos': np.zeros(14, np.float32)}
        output = context.step(old_state, [], frames(), runtime=SimpleNamespace(
            consumed_actions=0, control_epoch=1, config_version=1))
        context.step(old_state, output[1:], frames(1), runtime=SimpleNamespace(
            consumed_actions=1, control_epoch=1, config_version=1))
        if pipelined:
            assert context.pending is None
        before_calls = 2 if pipelined else 1
        threshold = 3.5 if source.endswith('lift2') else -1.2
        module.configure(context, active, 'params.gripper_threshold', str(threshold), lambda _: pytest.fail('no reset'))
        new_state = {'qpos': np.full(14, .3, np.float32)}
        # RC clears its action buffer but retains its consumed counter on commit.
        output = context.step(new_state, [], frames(9), runtime=SimpleNamespace(
            consumed_actions=1, control_epoch=2, config_version=2))
        expected = 0. if source.endswith('lift2') else .04
        np.testing.assert_allclose(output[:, [6, 13]], expected)
        np.testing.assert_array_equal(backend.calls[-1][0], new_state['qpos'])
        assert np.all(backend.calls[-1][1]['head'] == 9)
        assert len(backend.calls) == before_calls + 1
        assert context.history.tick == 1 and context.monitor()['reset_count'] == 0
        assert sum(name == 'load' for name, _ in backend.events) == 1
        assert not any(name == 'reset' for name, _ in backend.events)
        # Even setting the same value commits a new configuration generation.
        context.step(new_state, [], frames(10), runtime=SimpleNamespace(
            consumed_actions=1, control_epoch=3, config_version=3))
        assert context.history.tick == 1
    finally:
        backend.gate.set()
        context.close()


def test_backward_control_epoch_is_rejected(entry):
    module, active, _ = entry
    context = module.HistoryPolicy(config(), policy_config=active, backend=Backend())
    state = {'qpos': np.zeros(14, np.float32)}
    try:
        context.step(state, [], frames(), runtime=SimpleNamespace(
            consumed_actions=0, control_epoch=1, config_version=1))
        with pytest.raises(RuntimeError, match='backwards'):
            context.step(state, [], frames(), runtime=SimpleNamespace(
                consumed_actions=0, control_epoch=0, config_version=2))
    finally:
        context.close()
