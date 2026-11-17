"""Live threshold changes preserve both history instances and raw action plans."""
import copy
import importlib.util
from types import SimpleNamespace

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import ROOT
from test_qwen35_history_runtime import Backend, config, frames

GRIPPER_FIELDS = ('gripper_upper_threshold', 'gripper_upper_value', 'gripper_lower_threshold', 'gripper_lower_value')


@pytest.fixture(params=['qwen35_history', 'qwen35_history_lift2'])
def entry(request):
    source = ROOT / 'policy' / request.param
    spec = importlib.util.spec_from_file_location('threshold_' + request.param, source / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, read_config(source / 'policy.yaml.example'), request.param


def test_inclusive_rules_use_original_values_and_keep_unmatched_grippers(entry):
    module, active, _ = entry
    active['params'].update(gripper_upper_threshold=2., gripper_upper_value=-4.,
                            gripper_lower_threshold=-1., gripper_lower_value=5.)
    module.validate_config(active)
    backend = Backend()
    left = [-2., -1., 0., 2., 3.] * 10
    right = [3., 2., 0., -1., -2.] * 10
    backend.output[:, 6], backend.output[:, 13] = left, right
    raw = backend.output.copy()
    policy = module.HistoryPolicy(config(), policy_config=active, backend=backend)
    try:
        output = policy.step({'qpos':np.zeros(14)}, [], frames(),
                             runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        # High output -4 also satisfies the low rule; it must not be remapped.
        np.testing.assert_array_equal(output[:, 6], [5., 5., 0., -4., -4.] * 10)
        np.testing.assert_array_equal(output[:, 13], [-4., -4., 0., 5., 5.] * 10)
        np.testing.assert_array_equal(output[:, :6], raw[:, :6])
        np.testing.assert_array_equal(output[:, 7:13], raw[:, 7:13])
        np.testing.assert_array_equal(policy.canonical, output)
        np.testing.assert_array_equal(backend.output, raw)
    finally:
        policy.close()


@pytest.mark.parametrize('low_threshold', [2., 3.])
def test_upper_rule_wins_when_both_inclusive_rules_match(entry, low_threshold):
    module, active, _ = entry
    active['params'].update(gripper_upper_threshold=2., gripper_upper_value=4.,
                            gripper_lower_threshold=low_threshold, gripper_lower_value=-2.)
    module.validate_config(active)
    backend = Backend()
    backend.output[:, [6, 13]] = [2., 1.]
    policy = module.HistoryPolicy(config(), policy_config=active, backend=backend)
    try:
        result = policy.step({'qpos':np.zeros(14)}, [], frames(),
                             runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        np.testing.assert_array_equal(result[:, 6], 4.)
        np.testing.assert_array_equal(result[:, 13], -2.)
    finally:
        policy.close()


@pytest.mark.parametrize('field,raw,value', [
    ('gripper_upper_threshold', '2.5', 2.5),
    ('gripper_upper_value', '3.75', 3.75),
    ('gripper_lower_threshold', '-2.25', -2.25),
    ('gripper_lower_threshold', 'null', None),
    ('gripper_lower_value', '-3.25', -3.25),
])
def test_new_gripper_settings_accept_hot_candidates_without_lifecycle_effect(entry, field, raw, value):
    module, active, _ = entry
    before = copy.deepcopy(active)
    candidate, effects = copy.deepcopy(active), []
    module.configure(None, candidate, 'params.'+field, raw, effects.append)
    assert candidate['params'][field] == value
    assert effects == [] and active == before
    assert 'params.'+field in active['hot_reloadable']
    assert 'params.'+field in active['config_hints']['settings']


def test_existing_manifest_can_add_new_fields_without_updating_old_hints(entry):
    module, active, _ = entry
    for field in GRIPPER_FIELDS[1:]:
        active['params'].pop(field, None)
        active['hot_reloadable'].remove('params.'+field)
        active['config_hints']['settings'].pop('params.'+field, None)
    effects = []
    module.validate_config(active)
    module.configure(None, active, 'params.gripper_upper_value', '3.75', effects.append)
    module.configure(None, active, 'params.gripper_lower_threshold', '-2.25', effects.append)
    module.configure(None, active, 'params.gripper_lower_value', '-3.25', effects.append)
    assert [active['params'][key] for key in GRIPPER_FIELDS[1:]] == [3.75, -2.25, -3.25]
    assert effects == []


@pytest.mark.parametrize('pipelined', [False, True])
def test_threshold_commit_replans_grippers_without_reset_or_model_reload(entry, pipelined):
    module, active, source = entry
    lift2 = source.endswith('lift2')
    initial, updated = (2.5, 3.5) if lift2 else (-.8, -1.2)
    assert active['params']['gripper_upper_threshold'] == initial
    assert 'params.gripper_upper_threshold' in active['hot_reloadable']
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
        module.configure(context, candidate, 'params.gripper_upper_threshold', str(updated), effects.append)
        assert effects == [] and active['params']['gripper_upper_threshold'] == initial
        assert candidate['params']['gripper_upper_threshold'] == updated
        # Normal SDK commits update the existing dict; the context retains that reference.
        active['params'].update(candidate['params'])
        output = context.step(state, [], frames(1),
                              runtime=SimpleNamespace(consumed_actions=1, control_epoch=2, config_version=2))
        np.testing.assert_allclose(output[:, [6, 13]], np.tile([3., 4.9] if lift2 else [.04, .04], (len(output), 1)))
        np.testing.assert_array_equal(context.canonical, output)
        np.testing.assert_array_equal(output[:, :6], raw[:len(output), :6])
        assert context.policy_config is active and context.history.tick == 1
        assert len(backend.calls) == 2
        assert [name for name, _ in backend.events] == ['load', 'warmup', 'predict', 'predict']
    finally:
        context.close()


@pytest.mark.parametrize('raw', ['true', 'null', '"1.2"', 'NaN', 'Infinity', '-Infinity', '[]'])
@pytest.mark.parametrize('field', GRIPPER_FIELDS)
def test_gripper_candidate_validation_preserves_rejected_config(entry, field, raw):
    module, active, _ = entry
    candidate = copy.deepcopy(active)
    effects = []
    if field == 'gripper_lower_threshold' and raw == 'null':
        module.configure(None, candidate, 'params.'+field, raw, effects.append)
        assert candidate['params'][field] is None and effects == []
    else:
        with pytest.raises(ValueError, match=field):
            module.configure(None, candidate, 'params.'+field, raw, effects.append)
        assert candidate == active and effects == []


@pytest.mark.parametrize('value', [True, None, '1.2', float('nan'), float('inf'), []])
@pytest.mark.parametrize('field', GRIPPER_FIELDS)
def test_manifest_validates_gripper_rules_before_initialization(entry, field, value):
    module, active, _ = entry
    active['params'][field] = value
    if field == 'gripper_lower_threshold' and value is None:
        module.validate_config(active)
    else:
        with pytest.raises(ValueError, match=field):
            module.validate_config(active)


@pytest.mark.parametrize('field', ['gripper_upper_value', 'gripper_lower_value'])
def test_gripper_replacement_rejects_float32_overflow(entry, field):
    module, active, _ = entry
    candidate = copy.deepcopy(active)
    with pytest.raises(ValueError, match='finite float32'):
        module.configure(None, candidate, 'params.'+field, '1e100', lambda _: pytest.fail('no lifecycle effect'))
    assert candidate == active


def test_null_low_threshold_disables_lower_replacement(entry):
    module, active, _ = entry
    active['params'].update(gripper_upper_threshold=2., gripper_upper_value=4.,
                            gripper_lower_threshold=None, gripper_lower_value=-5.)
    backend = Backend()
    backend.output[:, [6,13]] = [1., 2.]
    context = module.HistoryPolicy(config(), policy_config=active, backend=backend)
    try:
        result = context.step({'qpos': np.zeros(14)}, [], frames(),
                              runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        np.testing.assert_array_equal(result[:, 6], 1.)
        np.testing.assert_array_equal(result[:, 13], 4.)
    finally:
        context.close()


def test_legacy_manifest_without_gripper_settings_uses_source_defaults(entry):
    module, active, source = entry
    for field in GRIPPER_FIELDS:
        active['params'].pop(field, None)
    module.validate_config(active)
    backend = Backend()
    backend.output[:, [6, 13]] = [2.5, 3.] if source.endswith('lift2') else [-.8, -.7]
    context = module.HistoryPolicy(config(), policy_config=active, backend=backend)
    try:
        output = context.step({'qpos': np.zeros(14, np.float32)}, [], frames(),
                              runtime=SimpleNamespace(consumed_actions=0, control_epoch=1))
        expected = [4.9, 4.9] if source.endswith('lift2') else [.04, .04]
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
        module.configure(context, active, 'params.gripper_upper_threshold', str(threshold), lambda _: pytest.fail('no reset'))
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
