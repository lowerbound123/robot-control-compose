"""Native LeRobot serialization/processors; real tiny-model smoke is explicitly synthetic."""
from config_helpers import read_config
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot.policies.pi05.configuration_pi05')

from pi05_helpers import CAMERAS, native_checkpoint_fixture, tiny_pi05_architecture, write_json

PACKAGE = Path(__file__).resolve().parents[1] / 'packages/pi05-policy/src'
if importlib.util.find_spec('rc_pi05_policy') is None:
    sys.path.insert(0, str(PACKAGE))


def implementation():
    assert importlib.util.find_spec('rc_pi05_policy.backend') is not None, 'Pi05 backend missing'
    return importlib.import_module('rc_pi05_policy.backend')


class Predictor(torch.nn.Module):
    """Replaces just expensive inference while real preprocessing runs."""
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.batch = None
        self.output = torch.zeros(1, config.chunk_size, 14)

    def predict_action_chunk(self, batch):
        self.batch = batch
        return self.output.clone()

    def reset(self): pass


def frames():
    image = np.zeros((2, 3, 3), np.uint8)
    image[..., 0] = 255
    return {name: image.copy() for name in CAMERAS}


def prepared(tmp_path, monkeypatch, *, relative=False, delta=False, affine=False, normalization='MEAN_STD'):
    module = implementation()
    path, model, _ = native_checkpoint_fixture(tmp_path, monkeypatch, relative=relative, normalization=normalization)
    if delta: model['actions']['representation'] = 'delta_joint'
    if affine:
        model['observations'].update(state_scale=[2.] * 14, state_offset=[1.] * 14)
    write_json(path, model)
    from rc_pi05_policy.configuration import load_model_config
    config = load_model_config(path, project_root=tmp_path)
    monkeypatch.setattr(module, 'load_local_policy', lambda checkpoint, cfg: Predictor(cfg))
    backend = module.Pi05Backend(config)
    backend.load()
    return backend


@pytest.mark.parametrize('normalization', ['MEAN_STD', 'QUANTILES'])
def test_real_processors_preserve_camera_keys_and_unnormalize_whole_chunk(tmp_path, monkeypatch, normalization):
    backend = prepared(tmp_path, monkeypatch, normalization=normalization)
    qpos = np.full(14, 3., np.float32)
    images = frames()
    try:
        result = backend.predict(qpos, images, 'pick up object')
        assert result.shape == (3, 14)  # Full model horizon=5, executable prefix=3.
        np.testing.assert_allclose(result, 10)
        batch = backend.policy.batch
        torch.testing.assert_close(batch['observation.state'], torch.ones(1, 14))
        assert batch['observation.images.head'].shape == (1, 3, 2, 3)
        torch.testing.assert_close(batch['observation.images.head'][0, 0], torch.ones(2, 3))
        assert not batch['observation.images.head'][0, 1:].any()
        assert 'observation.language.tokens' in batch
        np.testing.assert_equal(qpos, 3)
        np.testing.assert_equal(images['head'][..., 0], 255)
    finally: backend.close()


@pytest.mark.parametrize('relative,delta', [(False, False), (False, True), (True, False)])
def test_coordinates_and_relative_actions_are_converted_once(tmp_path, monkeypatch, relative, delta):
    backend = prepared(tmp_path, monkeypatch, relative=relative, delta=delta, affine=True)
    try:
        result = backend.predict(np.full(14, 3, np.float32), frames(), 'task')
        joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
        np.testing.assert_allclose(result[:, joints], 8 if relative or delta else 4.5)
        np.testing.assert_allclose(result[:, [6, 13]], 4.5)
    finally: backend.close()


def test_missing_action_statistics_fail_during_load(tmp_path, monkeypatch):
    module = implementation()
    path, _, _ = native_checkpoint_fixture(tmp_path, monkeypatch)
    from safetensors.torch import save_file
    post = read_config(tmp_path / 'weights/policy_postprocessor.json')
    step = next(s for s in post['steps'] if s['registry_name'] == 'unnormalizer_processor')
    save_file({'observation.state.mean': torch.ones(14)}, tmp_path / 'weights' / step['state_file'])
    from rc_pi05_policy.configuration import load_model_config
    monkeypatch.setattr(module, 'load_local_policy', lambda checkpoint, cfg: Predictor(cfg))
    backend = module.Pi05Backend(load_model_config(path, project_root=tmp_path))
    try:
        with pytest.raises(ValueError, match='action.*stat|stat.*action'):
            backend.load()
    finally: backend.close()


@pytest.mark.parametrize('keys,visual,missing', [
    ([], 'IDENTITY', 'observation.state'),
    (['observation.state', 'observation.images.old_head', *list(CAMERAS.values())[1:]],
     'MEAN_STD', 'observation.images.head'),
    (['observation.state', *CAMERAS.values()], 'MEAN_STD', None),
    (['observation.state'], 'IDENTITY', None),
])
def test_saved_normalization_filter_covers_required_features(tmp_path, monkeypatch, keys, visual, missing):
    module = implementation()
    path, _, cfg = native_checkpoint_fixture(tmp_path, monkeypatch)
    from lerobot.configs import NormalizationMode
    from safetensors.torch import load_file, save_file
    cfg.normalization_mapping['VISUAL'] = NormalizationMode(visual)
    cfg.save_pretrained(tmp_path / 'weights')
    pre_path = tmp_path / 'weights/policy_preprocessor.json'
    saved = read_config(pre_path)
    normalizer = next(s for s in saved['steps'] if s['registry_name'] == 'normalizer_processor')
    normalizer['config']['normalize_observation_keys'] = keys
    normalizer['config']['norm_map']['VISUAL'] = visual
    state_path = tmp_path / 'weights' / normalizer['state_file']
    stats = load_file(state_path)
    for camera in CAMERAS.values():
        stats[f'{camera}.mean'] = torch.full((3, 1, 1), .25)
        stats[f'{camera}.std'] = torch.full((3, 1, 1), .5)
    save_file(stats, state_path)
    write_json(pre_path, saved)
    from rc_pi05_policy.configuration import load_model_config
    monkeypatch.setattr(module, 'load_local_policy', lambda checkpoint, config: Predictor(config))
    backend = module.Pi05Backend(load_model_config(path, project_root=tmp_path))
    try:
        if missing:
            with pytest.raises(ValueError, match=f'{missing}.*normalize_observation_keys'):
                backend.load()
            assert backend.policy is None
        else:
            backend.load()
            backend.predict(np.full(14, 3, np.float32), frames(), 'task')
            torch.testing.assert_close(backend.policy.batch['observation.state'], torch.ones(1, 14))
            image = backend.policy.batch['observation.images.head'][0]
            torch.testing.assert_close(image[0], torch.full((2, 3), 1.5 if visual == 'MEAN_STD' else 1.))
            torch.testing.assert_close(image[1:], torch.full((2, 2, 3), -.5 if visual == 'MEAN_STD' else 0.))
    finally: backend.close()


@pytest.mark.parametrize('bad', ['shape', 'nan'])
def test_bad_model_output_never_reaches_rc(tmp_path, monkeypatch, bad):
    backend = prepared(tmp_path, monkeypatch)
    backend.policy.output = torch.zeros(1, 4, 14) if bad == 'shape' else torch.full((1, 5, 14), float('nan'))
    try:
        with pytest.raises(ValueError): backend.predict(np.zeros(14), frames(), 'task')
    finally: backend.close()


def test_affine_observation_overflow_is_rejected_before_inference(tmp_path, monkeypatch):
    backend = prepared(tmp_path, monkeypatch)
    backend.scale[:] = 3e38
    try:
        with pytest.raises(ValueError, match='finite|overflow'):
            backend.predict(np.full(14, 10, np.float32), frames(), 'task')
    finally: backend.close()


def test_strict_native_loading_rejects_corrupt_or_incomplete_weights(tmp_path, monkeypatch):
    from safetensors import SafetensorError
    module = implementation()
    _, _, cfg = native_checkpoint_fixture(tmp_path, monkeypatch)
    tiny_pi05_architecture(monkeypatch)
    with pytest.raises((SafetensorError, RuntimeError, ValueError),
                       match='[Hh]eader|[Dd]eserial|[Ss]afetensor'):
        module.load_local_policy(tmp_path / 'weights', cfg)
    from safetensors.torch import save_file
    save_file({'unexpected': torch.zeros(1)}, tmp_path / 'weights/model.safetensors')
    with pytest.raises(RuntimeError, match='[Mm]issing|[Uu]nexpected'):
        module.load_local_policy(tmp_path / 'weights', cfg)


def test_native_tiny_pi05_roundtrip_and_real_flow_matching(tmp_path, monkeypatch):
    module = implementation()
    path, _, cfg = native_checkpoint_fixture(tmp_path, monkeypatch)
    tiny_pi05_architecture(monkeypatch)
    from lerobot.policies.pi05.modeling_pi05 import PI05Policy
    original = PI05Policy(cfg)
    original.save_pretrained(tmp_path / 'weights')
    del original
    from rc_pi05_policy.configuration import load_model_config
    backend = module.Pi05Backend(load_model_config(path, project_root=tmp_path))
    try:
        backend.load()
        actions = backend.predict(np.zeros(14, np.float32), frames(), 'pick up object')
        assert actions.shape == (3, 14) and np.isfinite(actions).all()
        backend.reset_context()
    finally: backend.close()
    assert backend.policy is None
    backend.close()


def test_native_probe_verifies_loading_and_warmup_without_rc(tmp_path, monkeypatch, capsys):
    assert importlib.util.find_spec('rc_pi05_policy.probe') is not None, 'standalone Pi05 probe missing'
    probe = importlib.import_module('rc_pi05_policy.probe')
    path, _, cfg = native_checkpoint_fixture(tmp_path, monkeypatch)
    tiny_pi05_architecture(monkeypatch)
    from lerobot.policies.pi05.modeling_pi05 import PI05Policy
    PI05Policy(cfg).save_pretrained(tmp_path / 'weights')
    assert probe.main(['--config', str(path)]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['status'] == 'ready' and report['device'] == 'cpu'
    assert report['action_shape'] == [3, 14] and report['model_chunk_size'] == 5
    assert report['warmup_runs'] == 1 and report['synthetic_observations'] is True
    (tmp_path / 'weights/model.safetensors').unlink()
    assert probe.main(['--config', str(path)]) == 1
    assert 'model.safetensors' in capsys.readouterr().err
