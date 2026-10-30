from config_helpers import read_config
import importlib
import json
from pathlib import Path
import sys

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / 'packages/qwen35-policy/src'
sys.path.insert(0, str(PACKAGE))


def loader():
    assert (PACKAGE / 'rc_qwen35_policy/configuration.py').exists(), 'local model configuration is missing'
    return importlib.import_module('rc_qwen35_policy.configuration').load_model_config


def valid_stats():
    values = {'min': [-1] * 14, 'max': [1] * 14, 'mean': [0] * 14, 'std': [1] * 14}
    return {'arx_acone': {'observation.state': values, 'action': values}}


@pytest.fixture
def model_config(tmp_path):
    checkpoint = tmp_path / 'weights'
    checkpoint.mkdir()
    (checkpoint / 'config.json').write_text(json.dumps({'type': 'qwen3_5vla_ki_wan', 'chunk_size': 50}))
    (checkpoint / 'stats.json').write_text(json.dumps(valid_stats()))
    (checkpoint / 'model.safetensors').write_bytes(b'fixture')
    value = {'model': {'checkpoint': 'weights', 'workflow': 'single'},
             'runtime': {'instruction': 'pick up the cup'}}
    path = tmp_path / 'single.json'
    path.write_text(json.dumps(value))
    return path, value


def test_config_resolves_checkpoint_from_file_directory(model_config):
    path, _ = model_config
    config = loader()(path, workflow='single', project_root=path.parent)
    assert config['model']['checkpoint'] == str(path.parent / 'weights')
    assert config['model']['inference_backend'] == 'optimized'
    assert config['model']['dtype'] == 'bfloat16'
    assert config['execution']['inference_timeout_s'] == 60


def test_wrong_workflow_rejected_without_inheriting_checkpoint(model_config):
    path, config = model_config
    with pytest.raises(ValueError, match='workflow'):
        loader()(path, workflow='two_stage', project_root=path.parent)
    del config['model']['checkpoint']
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='checkpoint'):
        loader()(path, workflow='single', project_root=path.parent)


@pytest.mark.parametrize('change', [{'type': 'pi05'}, {'chunk_size': 49}])
def test_checkpoint_family_and_horizon_verified_before_unload(model_config, change):
    path, _ = model_config
    checkpoint = path.parent / 'weights/config.json'
    value = read_config(checkpoint)
    value.update(change)
    checkpoint.write_text(json.dumps(value))
    with pytest.raises(ValueError): loader()(path, workflow='single', project_root=path.parent)


@pytest.mark.parametrize('timeout', [0, True, float('nan')])
def test_timeout_must_be_positive_finite(model_config, timeout):
    path, value = model_config
    value['execution'] = {'inference_timeout_s': timeout}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError): loader()(path, workflow='single', project_root=path.parent)


def test_switch_model_returns_validated_cold_patch(model_config):
    path, _ = model_config
    config = loader()(path, workflow='single', project_root=path.parent)
    from rc_qwen35_policy.runtime import AsyncQwenPolicy
    from test_qwen35_runtime import Model
    policy = AsyncQwenPolicy(config, backend=Model(), project_root=path.parent)
    try:
        assert policy.switch_model('single.json') == {'params.model_config_path': str(path)}
        assert len(policy.backend.events) == 2, 'validation must not load or release a model'
        with pytest.raises(FileNotFoundError): policy.switch_model('missing.json')
    finally:
        policy.close()


@pytest.mark.parametrize('filename', ['pytorch_model.bin', 'pytorch_model.bin.index.json', 'model.safetensors.index.json'])
def test_preflight_rejects_weight_formats_the_loader_cannot_load(model_config, filename):
    path, _ = model_config
    checkpoint = path.parent / 'weights'
    (checkpoint / 'model.safetensors').unlink()
    if filename.endswith('.json'):
        (checkpoint / filename).write_text(json.dumps({'weight_map': {'weight': 'shard.bin'}}))
        (checkpoint / 'shard.bin').write_bytes(b'weights')
    else:
        (checkpoint / filename).write_bytes(b'weights')
    with pytest.raises((ValueError, FileNotFoundError), match='model.safetensors'):
        loader()(path, workflow='single', project_root=path.parent)


@pytest.mark.parametrize('invalid', ['missing_key', 'missing_statistic', 'wrong_width', 'nonnumeric'])
def test_stats_are_validated_before_requesting_model_unload(model_config, invalid):
    path, _ = model_config
    stats = valid_stats()
    if invalid == 'missing_key':
        stats = {}
    elif invalid == 'missing_statistic':
        del stats['arx_acone']['action']['std']
    elif invalid == 'wrong_width':
        stats['arx_acone']['action']['mean'] = [0] * 13
    else:
        stats['arx_acone']['action']['mean'] = ['invalid'] * 14
    (path.parent / 'weights/stats.json').write_text(json.dumps(stats))
    with pytest.raises(ValueError, match='stats'):
        loader()(path, workflow='single', project_root=path.parent)


@pytest.mark.parametrize('schema', ['misspelled_schema', 'franka'])
def test_incompatible_camera_schema_rejected_before_unload(model_config, schema):
    path, value = model_config
    value['model']['schema_key'] = schema
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='schema'):
        loader()(path, workflow='single', project_root=path.parent)


def test_supported_source_dual_arm_schema_still_loads(model_config):
    path, value = model_config
    value['model']['schema_key'] = 'arx_lift2'
    path.write_text(json.dumps(value))
    assert loader()(path, workflow='single', project_root=path.parent)['model']['schema_key'] == 'arx_lift2'


def test_preflight_schema_choices_match_bundled_three_camera_dual_arm_layout(monkeypatch):
    pytest.importorskip('torch')
    monkeypatch.syspath_prepend(str(PACKAGE.parents[2] / 'vendor/qwen35'))
    from lerobot.dataset_schemas import get_registry, get_schema
    from rc_qwen35_policy.statistics import SUPPORTED_SCHEMAS
    for name in SUPPORTED_SCHEMAS:
        assert name in get_registry().list_available()
        schema = get_schema(name)
        assert schema.image_mapping == {
            'images.rgb.head': 'observation.images.image0',
            'images.rgb.hand_left': 'observation.images.image1',
            'images.rgb.hand_right': 'observation.images.image2',
        }
        assert schema.action_mask_spec == [6, -1, 6, -1]
