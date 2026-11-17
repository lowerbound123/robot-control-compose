"""Signed gripper offsets belong to a new prediction, never a cached command."""
import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from config_helpers import read_config
from pi05_lift2_helpers import checkpoint_fixture
from rc_policy_sdk.transitions import TrajectoryAddTransition
from test_qwen35_history_runtime import Backend, config, frames
from test_qwen35_runtime import Model

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('qwen35_history', 'qwen35_history_lift2', 'pi05_lift2')


@pytest.fixture(params=SOURCES)
def entry(request):
    source = ROOT/'policy'/request.param
    spec = importlib.util.spec_from_file_location('delta_'+request.param, source/'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = read_config(source/'policy.yaml.example')
    manifest['params'].update(gripper_upper_threshold=2., gripper_lower_threshold=0.,
                              gripper_upper_value='+0.5', gripper_lower_value='-0.5')
    return module, manifest, request.param


@pytest.fixture
def running(entry, tmp_path):
    module, manifest, source = entry
    if source == 'pi05_lift2':
        path, *_ = checkpoint_fixture(tmp_path)
        model = Model()
        policy = module.AsyncPi05Policy(module.load_model_config(path, project_root=tmp_path),
                                       policy_config=manifest, backend=model)
    else:
        model = Backend()
        policy = module.HistoryPolicy(config(), policy_config=manifest, backend=model)
    model.output[:, :6] = .125
    model.output[:, 7:13] = .25
    model.output[:, [6, 13]] = [3., -1.]
    try:
        yield module, manifest, policy, model
    finally:
        model.gate.set()
        policy.close()


def step(policy, actions, count, *, epoch=1, version=1):
    return policy.step({'qpos':np.zeros(14, np.float32)}, actions, frames(),
                       runtime=SimpleNamespace(consumed_actions=count, control_epoch=epoch, config_version=version))


@pytest.mark.parametrize('raw,value,expected', [
    ('0.5', .5, [.5, .5]), ('-0.5', -.5, [-.5, -.5]),
    ('"+0.5"', '+0.5', [3.5, -.5]), ('"-0.5"', '-0.5', [2.5, -1.5]),
    ('"+5e-1"', '+5e-1', [3.5, -.5]), ('"-.5"', '-.5', [2.5, -1.5]),
])
def test_numbers_assign_and_signed_strings_offset_model_values(running, raw, value, expected):
    module, manifest, policy, model = running
    for name in ('gripper_upper_value', 'gripper_lower_value'):
        module.configure(None, manifest, 'params.'+name, raw, lambda _:pytest.fail('ordinary update'))
        assert type(manifest['params'][name]) is type(value) and manifest['params'][name] == value
    before = model.output.copy()
    output = step(policy, [], 0)
    np.testing.assert_allclose(output[:, [6,13]], np.tile(expected, (50,1)))
    np.testing.assert_array_equal(output[:, :6], before[:, :6])
    np.testing.assert_array_equal(output[:, 7:13], before[:, 7:13])
    np.testing.assert_array_equal(model.output, before)
    np.testing.assert_array_equal(policy.canonical, output)


def test_cached_prefixes_and_identical_new_predictions_never_accumulate_delta(running):
    _, _, policy, model = running
    chunk = step(policy, [], 0)
    for count in range(1, 5):
        expected = chunk[1:].copy()
        chunk = step(policy, chunk[1:], count)
        np.testing.assert_array_equal(chunk, expected)
    assert len(model.calls) == 1
    for count in (50, 100, 150):
        chunk = step(policy, [], count)
        np.testing.assert_allclose(chunk[:, [6,13]], np.tile([3.5,-1.5], (50,1)))
    assert len(model.calls) == 4
    np.testing.assert_array_equal(model.output[:, 6], 3.)


def test_inflight_prediction_uses_submission_rules_and_new_epoch_uses_new_rules(running):
    _, manifest, policy, model = running
    model.gate.clear()
    with ThreadPoolExecutor(1) as caller:
        future = caller.submit(step, policy, [], 0)
        assert model.started.wait(1)
        # Change the live dictionary after submission to exercise snapshot ownership.
        manifest['params']['gripper_upper_value'] = '+2.0'
        model.gate.set()
        output = future.result(3)
    np.testing.assert_array_equal(output[:, 6], 3.5)
    fresh = step(policy, [], 1, epoch=2, version=2)
    np.testing.assert_array_equal(fresh[:, 6], 5.)
    assert sum(name == 'load' for name, _ in model.events) == 1


@pytest.mark.parametrize('value', ['0.5', '', '+', '--0.5', '+1+2', '+NaN', '-Infinity',
                                   '+1e100', '+ 0.5', ' +0.5', '+0.5 ', '+1_000'])
def test_invalid_delta_rejects_candidate_without_mutating_active_config(entry, value):
    module, manifest, _ = entry
    for name in ('gripper_upper_value', 'gripper_lower_value'):
        before = copy.deepcopy(manifest)
        with pytest.raises(ValueError, match=name):
            module.configure(None, manifest, 'params.'+name, json.dumps(value), lambda _:pytest.fail('no effect'))
        assert manifest == before


def test_addition_overflow_rejects_prediction_without_changing_model_output(running):
    _, manifest, policy, model = running
    manifest['params']['gripper_upper_value'] = '+3e38'
    model.output[:, 6] = np.float32(3e38)
    before = model.output.copy()
    with pytest.raises(ValueError, match='float32|finite'):
        step(policy, [], 0)
    np.testing.assert_array_equal(model.output, before)
    assert policy._failure is not None


def test_upper_priority_and_no_cascading_delta_or_overflow_on_unmatched_rules(running):
    _, manifest, policy, model = running
    manifest['params'].update(gripper_upper_threshold=2., gripper_upper_value='-3.0',
                              gripper_lower_threshold=2., gripper_lower_value='+3.0')
    model.output[:, [6,13]] = [2., 1.]
    output = step(policy, [], 0)
    np.testing.assert_allclose(output[:, [6,13]], np.tile([-1.,4.], (50,1)))
    # A delta for a rule that never wins must not produce a spurious overflow.
    manifest['params'].update(gripper_upper_value=0., gripper_lower_value='+3e38',
                              gripper_lower_threshold=1e40)
    model.output[:, [6,13]] = np.float32(3e38)
    output = step(policy, [], 50, epoch=2, version=2)
    np.testing.assert_array_equal(output[:, [6,13]], 0.)


def test_disabled_lower_rule_keeps_raw_values_and_monitor_remains_preprocessing(running):
    _, manifest, policy, model = running
    manifest['params']['gripper_lower_threshold'] = None
    output = step(policy, [], 0)
    np.testing.assert_array_equal(output[:,6],3.5)
    np.testing.assert_array_equal(output[:,13],-1.)
    monitor = getattr(policy, 'monitor', lambda: {})()
    if 'left_gripper_raw' in monitor:
        assert monitor['left_gripper_raw'] == str([3.] * 50)
        assert monitor['right_gripper_raw'] == str([-1.] * 50)


@pytest.mark.parametrize('threshold', [-.8, -1.2, .1, .3])
def test_float32_threshold_equality_is_preserved_for_upper_and_lower_rules(running, threshold):
    _, manifest, policy, model = running
    manifest['params'].update(gripper_upper_threshold=threshold, gripper_lower_threshold=threshold)
    model.output[:, [6,13]] = threshold
    output = step(policy, [], 0)
    np.testing.assert_allclose(output[:,[6,13]],model.output[:,[6,13]]+.5,rtol=1e-6)
    manifest['params']['gripper_upper_threshold'] = threshold+1
    output = step(policy, [], 50, epoch=2, version=2)
    np.testing.assert_allclose(output[:,[6,13]],model.output[:,[6,13]]-.5,rtol=1e-6)


@pytest.mark.parametrize('source', SOURCES[:2])
@pytest.mark.parametrize('trunk_mode,smoothing', [('tail',False),('tail',True),('close',True),('now',True)])
@pytest.mark.parametrize('replacement', ['+0.5', 4.])
def test_history_processes_only_new_prediction_before_fusion(source, trunk_mode, smoothing, replacement):
    spec = importlib.util.spec_from_file_location('fused_'+source, ROOT/'policy'/source/'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = read_config(ROOT/'policy'/source/'policy.yaml.example')
    manifest['params'].update(gripper_upper_threshold=2., gripper_upper_value=replacement,
                              gripper_lower_threshold=None)
    model = Backend()
    model.output[:, 0] = np.arange(50)
    model.output[:, [6,13]] = 3.
    settings = config(45, True)
    settings['transition'] = dict(trunk_mode=trunk_mode, trunk_smoothing=smoothing,
                                  max_smoothing_steps=2, exponential_factor=5., exponential_smoothing=.25)
    policy = module.HistoryPolicy(settings, backend=model, policy_config=manifest)
    old = np.zeros((5,14), np.float32)
    old[:, 0], old[:, [6,13]] = np.arange(5), 3.5
    original = old.copy()
    old.flags.writeable = False
    new = model.output.copy()
    new[:, [6,13]] = 3.5 if isinstance(replacement,str) else 4.
    expected = TrajectoryAddTransition(**settings['transition']).apply(old,new)
    try:
        result = step(policy, old, 100)
        np.testing.assert_allclose(result, expected)
        np.testing.assert_array_equal(policy.canonical,result)
        np.testing.assert_array_equal(old,original)
        np.testing.assert_array_equal(model.output[:, [6,13]],3.)
    finally:
        policy.close()
