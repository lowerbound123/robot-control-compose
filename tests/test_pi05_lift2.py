"""Independent LIFT2 Pi05 source, with only expensive model inference replaced."""
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import copy
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from config_helpers import read_config
from pi05_helpers import checkpoint_fixture
from project_templates import copy_templates
from test_qwen35_runtime import Model, images

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'policy/pi05_lift2'


def entry():
    assert (SOURCE/'main.py').is_file(), 'independent pi05_lift2 source missing'
    sys.path.insert(0, str(SOURCE/'src'))
    spec = importlib.util.spec_from_file_location('pi05_lift2_entry', SOURCE/'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime(count, epoch=1, version=1):
    return SimpleNamespace(consumed_actions=count, control_epoch=epoch, config_version=version)


@pytest.fixture
def running_policy(tmp_path):
    module = entry()
    path, *_ = checkpoint_fixture(tmp_path)
    settings = module.load_model_config(path, project_root=tmp_path)
    manifest = read_config(SOURCE/'policy.yaml.example')
    model = Model()
    policy = module.AsyncPi05Policy(settings, policy_config=manifest, backend=model, project_root=tmp_path)
    try:
        yield module, manifest, policy, model
    finally:
        model.gate.set()
        policy.close()


def step(policy, chunk, count, epoch=1, version=1):
    return policy.step({'qpos':np.arange(14)}, chunk, images(), runtime=runtime(count, epoch, version))


def test_new_source_is_discoverable_and_reuses_only_pi05_dependencies(tmp_path):
    from robot_control.config import PolicyRegistry
    from robot_control.yaml_config import load_yaml_config
    from setup_project import build_fingerprint, select_policies
    assert SOURCE.is_dir(), 'independent pi05_lift2 source missing'
    for identity in ('pi05', 'pi05_lift2'):
        copy_templates(ROOT/'policy'/identity, tmp_path/'policy'/identity)
    registry = PolicyRegistry(tmp_path/'policy')
    assert registry.get('pi05_lift2').config['id'] == 'pi05_lift2'
    assert select_policies(tmp_path, ['pi05_lift2']) == ['pi05_lift2']
    services = [load_yaml_config(ROOT/'policy'/name/'compose.yaml')['services']['policy']
                for name in ('pi05', 'pi05_lift2')]
    assert services[1]['image'] == services[0]['image'] == 'robot-control-compose-pi05:local'
    assert services[1]['environment']['RC_SOURCE_MODULES'] == '{"rc_pi05_lift2":"/workspace/policy/pi05_lift2/src"}'
    mounts = {row['target']:row for row in services[1]['volumes']}
    assert mounts['/workspace/policy/pi05_lift2/src']['read_only']
    fingerprints = []
    for name, service in zip(('pi05','pi05_lift2'), services):
        build = copy.deepcopy(service['build'])
        build['context'] = str(ROOT)
        build['additional_contexts']['policy-deps'] = str(ROOT/'policy'/name/'dependencies')
        fingerprints.append(build_fingerprint(build))
    assert fingerprints[0] == fingerprints[1]


def test_default_lift2_grippers_are_processed_without_changing_model_or_cached_values(running_policy):
    _, _, policy, model = running_policy
    model.output[:, 6] = np.tile([2.,2.5,3.,1.,4.], 10)
    model.output[:, 13] = np.tile([3.,2.,2.5,4.,1.], 10)
    before = model.output.copy()
    chunk = step(policy, [], 0)
    np.testing.assert_allclose(chunk[:, 6], [0.,4.9,4.9,0.,4.9]*10)
    np.testing.assert_allclose(chunk[:, 13], [4.9,0.,4.9,4.9,0.]*10)
    np.testing.assert_array_equal(policy.canonical, chunk)
    np.testing.assert_array_equal(model.output, before)
    cached = step(policy, chunk[1:], 1)
    np.testing.assert_array_equal(cached, chunk[1:])
    assert len(model.calls) == 1


def test_gripper_hot_commit_discards_old_epoch_plan_without_reloading_model(running_policy):
    module, manifest, policy, model = running_policy
    model.output[:, [6,13]] = [3.,2.]
    first = step(policy, [], 0)
    assert first.shape == (50,14)
    effects = []
    module.configure(policy, manifest, 'params.gripper_upper_value', '1.5', effects.append)
    module.configure(policy, manifest, 'params.gripper_lower_value', '0.25', effects.append)
    fresh = step(policy, [], 1, epoch=2, version=2)
    np.testing.assert_array_equal(fresh[:, 6], 1.5)
    np.testing.assert_array_equal(fresh[:, 13], .25)
    assert effects == [] and policy.policy_config is manifest
    assert [name for name, _ in model.events].count('load') == 1
    assert len(model.calls) == 2


def test_null_lower_threshold_keeps_unmatched_raw_grippers(running_policy):
    module, manifest, policy, model = running_policy
    module.configure(policy, manifest, 'params.gripper_lower_threshold', 'null', lambda _: pytest.fail('no lifecycle'))
    model.output[:, [6,13]] = [1.,3.]
    chunk = step(policy, [], 0)
    np.testing.assert_array_equal(chunk[:,6],1.)
    np.testing.assert_allclose(chunk[:,13],4.9)


def test_scheduling_only_config_version_keeps_current_plan(running_policy):
    _, _, policy, model = running_policy
    chunk = step(policy, [], 0)
    result = step(policy, chunk[1:], 1, version=2)
    np.testing.assert_array_equal(result, chunk[1:])
    assert len(model.calls) == 1 and not any(name == 'reset' for name, _ in model.events)


@pytest.mark.parametrize('value,expected', [('1.5',1.5), ('"+0.75"',3.75)])
def test_hot_config_drains_pending_inference_and_uses_fresh_observation(running_policy, value, expected):
    module, manifest, policy, model = running_policy
    chunk = step(policy, [], 0)
    model.gate.clear()
    model.started.clear()
    step(policy, chunk[21:], 21)
    assert model.started.wait(1) and policy.pending is not None
    module.configure(policy, manifest, 'params.gripper_upper_value', value, lambda _: pytest.fail('no lifecycle'))
    model.output[:, [6,13]] = [3.,2.]
    with ThreadPoolExecutor(1) as caller:
        changed = caller.submit(policy.step, {'qpos':np.full(14,9.)}, [], images(), runtime=runtime(22,2,2))
        try:
            assert not changed.done()
        finally:
            model.gate.set()
        fresh = changed.result(3)
    assert len(model.calls) == 3
    np.testing.assert_array_equal(model.calls[-1][0], 9.)
    np.testing.assert_array_equal(fresh[:,6], expected)
    assert sum(name == 'load' for name, _ in model.events) == 1


def test_unexplained_or_backward_generation_is_not_a_hot_config_reset(running_policy):
    _, _, policy, _ = running_policy
    step(policy, [], 0, epoch=2)
    with pytest.raises(RuntimeError, match='epoch|generation'):
        step(policy, [], 1, epoch=1, version=2)
    with pytest.raises(RuntimeError, match='epoch|generation'):
        step(policy, [], 1, epoch=3, version=1)


@pytest.mark.parametrize('field,raw', [('gripper_upper_value','true'), ('gripper_lower_value','1e100'),
    ('gripper_lower_threshold','"2"'), ('gripper_upper_threshold','NaN')])
def test_invalid_gripper_config_rejects_candidate_without_side_effects(field, raw):
    module = entry()
    active = read_config(SOURCE/'policy.yaml.example')
    before = copy.deepcopy(active)
    with pytest.raises(ValueError, match=field):
        module.configure(None, active, 'params.'+field, raw, lambda _: pytest.fail('no lifecycle effect'))
    assert active == before


def test_test_scope_registers_only_selected_pi05_sources():
    from test_scope import POLICIES, suite
    assert 'pi05_lift2' in POLICIES
    chosen = suite({'pi05_lift2'}, False)
    assert 'tests/test_pi05_lift2.py' in chosen
    assert 'tests/test_pi05_worker.py' not in chosen
    assert 'tests/test_pi05_lift2.py' not in suite({'pi05'}, False)


def test_delta_keeps_old_overlap_while_new_future_is_pending_and_after_collection(running_policy):
    _, manifest, policy, model = running_policy
    manifest['params'].update(gripper_upper_threshold=2., gripper_lower_threshold=2.,
                              gripper_upper_value='+0.5', gripper_lower_value='-0.5')
    model.output[:, [6,13]] = [3.,1.]
    chunk = step(policy, [], 0)
    model.gate.clear()
    model.started.clear()
    model.output[:, [6,13]] = [8.,4.]
    chunk = step(policy, chunk[21:], 21)
    assert model.started.wait(1)
    chunk = step(policy, chunk[1:], 22)
    np.testing.assert_allclose(chunk[:,[6,13]],np.tile([3.5,.5],(28,1)))
    model.gate.set()
    policy.pending.result(3)
    output = step(policy, chunk[1:], 23)
    np.testing.assert_allclose(output[:27,[6,13]],np.tile([3.5,.5],(27,1)))
    np.testing.assert_allclose(output[27:,[6,13]],np.tile([8.5,4.5],(len(output)-27,1)))
    np.testing.assert_array_equal(policy.canonical,output)
    np.testing.assert_array_equal(model.output[:,6],8.)


def test_gripper_processing_is_included_in_prediction_completion_deadline(running_policy, monkeypatch):
    import time
    import rc_pi05_lift2.runtime as implementation
    _, _, policy, _ = running_policy
    # Model completion uses the SDK clock. Only the postprocessing completion
    # clock is advanced, making the deadline check independent of wall time.
    monkeypatch.setattr(implementation, 'time', SimpleNamespace(monotonic=lambda:time.monotonic()+120))
    with pytest.raises(TimeoutError,match='deadline'):
        step(policy, [], 0)
    assert policy._failure is not None and len(policy.canonical)==0
