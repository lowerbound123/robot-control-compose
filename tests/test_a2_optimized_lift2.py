"""Robot-specific binding of the shared A2 runtime."""
import copy
import json

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import ROOT, model_file
from rc_policy_sdk.worker import _load_policy
from test_qwen35_history_lift2_normalization import (
    ACTION_MEAN, ACTION_STD, MODEL_COLUMNS, STATE_MEAN, STATE_STD, mixed_checkpoint,
)
from test_qwen35_history_backend import isolated_vendor
from test_qwen35_history_runtime import Backend, config, frames, step

SOURCE = ROOT / 'policy/a2_optimized_lift2'


def entry():
    return _load_policy(SOURCE, '_a2_lift2_test')


def test_lift2_preflight_and_model_changes_require_lift2_statistics(tmp_path, monkeypatch):
    module = entry()
    path = model_file(tmp_path)
    stats = path.parent / 'weights/stats.json'
    acone_only = stats.read_text()
    manifest = read_config(SOURCE / 'policy.yaml.example')
    monkeypatch.setattr(module, 'POLICY_ROOT', tmp_path)
    policy = module.OptimizedPolicy
    monkeypatch.setattr(module, 'OptimizedPolicy', lambda model, **kwargs: policy(model, backend=Backend(), **kwargs))
    with pytest.raises(ValueError, match='arx_lift2'):
        module.initialize(manifest, runtime=None)
    stats.write_text(json.dumps({'arx_lift2': json.loads(acone_only)['arx_acone']}))
    context = module.initialize(manifest, runtime=None)
    try:
        assert context.robot_type == context.monitor()['robot_type'] == 'arx_lift2'
        candidate, effects = copy.deepcopy(manifest), []
        module.configure(context, candidate, 'params.model_config_path', str(path), effects.append)
        assert effects == ['reinitialize']
        assert module.switch_model(context, str(path)) == {'params.model_config_path': path.name}
        effects.clear()
        module.configure(context, candidate, 'model_config.transition.trunk_mode', 'close', effects.append)
        assert not effects and module.export_config(context, candidate)['files'][0]['data']['transition']['trunk_mode'] == 'close'
        with pytest.raises(ValueError, match='close or tail'):
            module.configure(context, candidate, 'model_config.transition.trunk_mode', 'now', effects.append)
        stats.write_text(acone_only)
        with pytest.raises(ValueError, match='arx_lift2'):
            module.switch_model(context, str(path))
        with pytest.raises(ValueError, match='arx_lift2'):
            module.configure(context, candidate, 'params.model_config_path', str(path), effects.append)
    finally:
        context.close()


def test_lift2_uses_gripper_defaults_without_explicit_params():
    backend = Backend()
    policy = entry().OptimizedPolicy({**config(), 'inference': {'generate_language': []}}, backend=backend)
    try:
        backend.output[:, 6] = 2.49
        backend.output[:, 13] = 2.5
        actions = step(policy, [], 0)
        np.testing.assert_array_equal(actions[:, 6], 0.)
        np.testing.assert_allclose(actions[:, 13], 4.9)
        np.testing.assert_array_equal(actions[:, :6], backend.output[:, :6])
        policy.policy_config['params'] = {'gripper_lower_threshold': None, 'gripper_upper_value': '+0.5'}
        actions = step(policy, [], 1)
        np.testing.assert_array_equal(actions[:, 6], backend.output[:, 6])
        np.testing.assert_allclose(actions[:, 13], 3.)
    finally:
        policy.close()


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('action_mode', ['delta', 'abs'])
def test_lift2_backend_uses_native_state_and_action_statistics(mixed_checkpoint, monkeypatch, action_mode):
    torch = pytest.importorskip('torch')
    from rc_qwen35_history.a2.backend import OptimizedBackend

    module = entry()
    train = mixed_checkpoint.parent / 'weights/train_config.json'
    raw = json.loads(train.read_text())
    raw['dataset']['action_mode'] = action_mode
    train.write_text(json.dumps(raw))
    monkeypatch.setattr(module, 'POLICY_ROOT', mixed_checkpoint.parent)
    monkeypatch.setattr(OptimizedBackend, 'load', OptimizedBackend.prepare_transforms)
    monkeypatch.setattr(OptimizedBackend, 'warmup', lambda self: None)
    manifest = {'execution': {'control_mode': 'qpos'}, 'params': {'model_config_path': mixed_checkpoint.name}}
    context = module.initialize(manifest, runtime=None)
    try:
        backend = context.backend
        assert isinstance(backend, OptimizedBackend) and backend.schema.robot_type == 'arx_lift2'
        qpos = np.arange(14, dtype=np.float32) + 5
        observed = backend.prepare_sample(qpos, frames(), 'test')['observation.state']
        expected = np.zeros(32, np.float32)
        expected[MODEL_COLUMNS] = (qpos - STATE_MEAN) / (STATE_STD + 1e-6)
        np.testing.assert_allclose(observed, expected, rtol=1e-6)
        prediction = (torch.arange(32, dtype=torch.float32) / 10).repeat(1, 50, 1)
        actions = backend.finalize_actions(prediction, qpos)
        expected = (np.array(MODEL_COLUMNS) / 10) * (ACTION_STD + 1e-6) + ACTION_MEAN
        if action_mode == 'delta':
            expected += np.where(np.isin(np.arange(14), [6, 13]), 0, qpos)
        np.testing.assert_allclose(actions, np.tile(expected, (50, 1)), rtol=1e-6, atol=1e-6)
    finally:
        context.close()


def test_lift2_is_discoverable_with_own_config_and_shared_a2_source(tmp_path):
    from project_templates import copy_templates
    from robot_control.config import PolicyRegistry

    copy_templates(SOURCE, tmp_path / SOURCE.name)
    registry = PolicyRegistry(tmp_path)
    record = registry.get(SOURCE.name)
    assert record.config['id'] == SOURCE.name and record.config['execution']['control_mode'] == 'qpos'
    params = record.config['params']
    assert [params['gripper_' + key] for key in ('upper_threshold', 'upper_value', 'lower_threshold', 'lower_value')] == [
        2.5, 4.9, 2.5, 0.]
    manifest = read_config(SOURCE / 'compose.yaml')['services']['policy']
    original = read_config(ROOT / 'policy/a2_optimized/compose.yaml')['services']['policy']
    assert manifest == original
    assert record.config['config_hints']['settings']['model_config.transition.trunk_mode']['choices'] == ['close', 'tail']
