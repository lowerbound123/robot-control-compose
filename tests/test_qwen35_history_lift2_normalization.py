"""LIFT2 states and actions must use their own checkpoint statistics end to end."""
import copy
import json

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import model_file
from test_qwen35_history_backend import isolated_vendor
from test_qwen35_history_lift2 import SOURCE, entry
from test_qwen35_history_runtime import Backend, frames


STATE_MEAN = np.arange(14, dtype=np.float32) + 10
STATE_STD = np.arange(14, dtype=np.float32) / 10 + 2
ACTION_MEAN = np.arange(14, dtype=np.float32) / 5 - 1
ACTION_STD = np.arange(14, dtype=np.float32) / 10 + .5
MODEL_COLUMNS = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 15]


@pytest.fixture
def mixed_checkpoint(tmp_path):
    path = model_file(tmp_path)
    stats_path = tmp_path / 'weights/stats.json'
    stats = json.loads(stats_path.read_text())
    lift2 = {}
    for prefix, means, stds in [('states', STATE_MEAN, STATE_STD),
                               ('actions', ACTION_MEAN, ACTION_STD)]:
        for name, start, end in [('left_joint', 0, 6), ('left_gripper', 6, 7),
                                 ('right_joint', 7, 13), ('right_gripper', 13, 14)]:
            lift2[f'{prefix}.{name}.position'] = {
                'mean': means[start:end].tolist(), 'std': stds[start:end].tolist()}
    stats['arx_lift2'] = lift2
    stats_path.write_text(json.dumps(stats))
    return path


def test_checkpoint_selects_lift2_statistics_and_keeps_acone_default(mixed_checkpoint):
    from rc_qwen35_history.configuration import checkpoint_assets
    directory = mixed_checkpoint.parent / 'weights'
    _, _, lift2 = checkpoint_assets(directory, robot_type='arx_lift2')
    _, _, acone = checkpoint_assets(directory)
    for prefix, mean, std in [('states', STATE_MEAN, STATE_STD),
                              ('actions', ACTION_MEAN, ACTION_STD)]:
        np.testing.assert_array_equal(lift2[prefix]['mean'], mean)
        np.testing.assert_array_equal(lift2[prefix]['std'], std)
        np.testing.assert_array_equal(acone[prefix]['mean'], np.ones(14))
        np.testing.assert_array_equal(acone[prefix]['std'], np.full(14, 2))


@pytest.mark.parametrize('broken', ['missing', 'shape', 'negative_std', 'nonfinite', 'unknown_robot'])
def test_invalid_lift2_statistics_never_fall_back_to_valid_acone(mixed_checkpoint, broken):
    from rc_qwen35_history.configuration import checkpoint_assets
    stats_path = mixed_checkpoint.parent / 'weights/stats.json'
    data = json.loads(stats_path.read_text())
    if broken == 'missing':
        del data['arx_lift2']
    elif broken == 'shape':
        data['arx_lift2']['states.left_joint.position']['mean'] = [0.]
    elif broken == 'negative_std':
        data['arx_lift2']['actions.right_gripper.position']['std'] = [-1.]
    elif broken == 'nonfinite':
        data['arx_lift2']['states.right_gripper.position']['mean'] = [float('inf')]
    stats_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        checkpoint_assets(stats_path.parent,
                          robot_type='unknown' if broken == 'unknown_robot' else 'arx_lift2')


@pytest.mark.parametrize('has_lift2', [True, False])
def test_initialize_configure_and_switch_model_bind_lift2_preflight(tmp_path, monkeypatch, has_lift2):
    from rc_qwen35_history import backend as shared_backend
    module = entry()
    path = model_file(tmp_path, robot_type='arx_lift2' if has_lift2 else 'arx_acone')
    manifest = read_config(SOURCE / 'policy.yaml.example')
    manifest['params']['model_config_path'] = str(path)
    selected = []

    def fake_gpu(config, *, robot_type):
        selected.append(robot_type)
        return Backend()

    monkeypatch.setattr(shared_backend, 'HistoryBackend', fake_gpu)
    if not has_lift2:
        with pytest.raises(ValueError, match='arx_lift2'):
            module.initialize(manifest, runtime=None)
        assert not selected, 'preflight must reject before any model is loaded'
        # Build a context with a supplied model to exercise switch-model preflight separately.
        from test_qwen35_history_runtime import config
        context = module.HistoryPolicy(config(), backend=Backend(), project_root=tmp_path)
    else:
        context = module.initialize(manifest, runtime=None)
        assert selected == ['arx_lift2']
    try:
        candidate = copy.deepcopy(manifest)
        effects = []
        if has_lift2:
            module.configure(context, candidate, 'params.model_config_path', str(path), effects.append)
            assert effects == ['reinitialize']
            assert module.switch_model(context, str(path)) == {'params.model_config_path': str(path)}
        else:
            with pytest.raises(ValueError, match='arx_lift2'):
                module.configure(context, candidate, 'params.model_config_path', str(path), effects.append)
            assert candidate == manifest and effects == []
            with pytest.raises(ValueError, match='arx_lift2'):
                module.switch_model(context, str(path))
    finally:
        context.close()


@pytest.mark.parametrize('action_mode', ['delta', 'abs'])
def test_real_transforms_use_lift2_state_and_action_stats_and_preserve_layout(mixed_checkpoint, action_mode):
    torch = pytest.importorskip('torch')
    from rc_qwen35_history.backend import HistoryBackend
    module = entry()
    train_path = mixed_checkpoint.parent / 'weights/train_config.json'
    train = json.loads(train_path.read_text())
    train['dataset']['action_mode'] = action_mode
    train_path.write_text(json.dumps(train))
    config = module.load_model_config(mixed_checkpoint, project_root=mixed_checkpoint.parent)
    backend = HistoryBackend(config, robot_type=module.HistoryPolicy.robot_type)
    backend.prepare_transforms()
    assert backend.schema.robot_type == 'arx_lift2'

    qpos = np.arange(14, dtype=np.float32) + 5
    before = qpos.copy()
    sample = backend.prepare_sample(qpos, frames(), 'task')
    expected_state = np.zeros(32, np.float32)
    expected_state[MODEL_COLUMNS] = (qpos - STATE_MEAN) / (STATE_STD + 1e-6)
    np.testing.assert_allclose(sample['observation.state'], expected_state, rtol=1e-6)
    assert all(f'observation.images.image{i}' in sample for i in range(3))

    prediction = (torch.arange(32, dtype=torch.float32) / 10).repeat(1, 50, 1)
    actions = backend.finalize_actions(prediction, qpos)
    expected = (np.array(MODEL_COLUMNS) / 10) * (ACTION_STD + 1e-6) + ACTION_MEAN
    if action_mode == 'delta':
        expected += np.where(np.isin(np.arange(14), [6, 13]), 0, qpos)
    np.testing.assert_allclose(actions, np.tile(expected, (50, 1)), rtol=1e-6, atol=1e-6)
    processed = module.HistoryPolicy._process(actions)
    np.testing.assert_array_equal(processed[:, 6], 0)
    np.testing.assert_allclose(processed[:, 13], 4.9)
    np.testing.assert_array_equal(qpos, before)
