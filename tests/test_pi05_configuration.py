import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pi05_helpers import CAMERAS, checkpoint_fixture, write_json

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'packages/pi05-policy/src'
if importlib.util.find_spec('rc_pi05_policy') is None:
    sys.path.insert(0, str(PACKAGE))


def implementation():
    assert importlib.util.find_spec('rc_pi05_policy.configuration') is not None, 'Pi05 configuration loader missing'
    return importlib.import_module('rc_pi05_policy.configuration')


def load(path):
    return implementation().load_model_config(path.name, project_root=path.parent)


def test_checkpoint_horizon_and_explicit_camera_names_are_preserved(tmp_path):
    path, _, _, _, _ = checkpoint_fixture(tmp_path)
    cfg = load(path)
    assert cfg['model']['checkpoint'] == str(tmp_path / 'weights')
    assert cfg['model']['tokenizer_path'] == str(tmp_path / 'tokenizer')
    assert cfg['execution']['chunk_size'] == 50
    assert cfg['execution']['request_when_remaining'] == 30
    assert cfg['observations']['camera_map'] == CAMERAS
    assert cfg['actions']['representation'] == 'absolute'


def test_executable_prefix_drives_shared_scheduler_horizon(tmp_path):
    path, _, _, _, _ = checkpoint_fixture(tmp_path, horizon=50, execute=10)
    cfg = load(path)
    assert cfg['model']['chunk_size'] == 50
    assert cfg['execution']['chunk_size'] == 10
    assert cfg['execution']['request_when_remaining'] == 6


@pytest.mark.parametrize('field,value,match', [
    ('type', 'pi0', 'pi05'), ('n_action_steps', 51, 'n_action_steps'),
    ('chunk_size', 0, 'chunk_size'), ('rtc_config', {'enabled': True}, 'RTC'),
    ('use_visual_memory', True, 'memory'), ('empty_cameras', 1, 'camera'),
])
def test_unsupported_checkpoint_rejected_before_model_import(tmp_path, field, value, match):
    path, _, cfg, _, _ = checkpoint_fixture(tmp_path)
    cfg[field] = value
    write_json(tmp_path / 'weights/config.json', cfg)
    with pytest.raises(ValueError, match=match): load(path)


@pytest.mark.parametrize('section,key', [('input_features', 'observation.state'), ('output_features', 'action')])
def test_other_robot_dimensions_are_not_silently_truncated(tmp_path, section, key):
    path, _, cfg, _, _ = checkpoint_fixture(tmp_path)
    cfg[section][key]['shape'] = [7]
    write_json(tmp_path / 'weights/config.json', cfg)
    with pytest.raises(ValueError, match='14'): load(path)


@pytest.mark.parametrize('filename', [
    'weights/model.safetensors', 'weights/policy_preprocessor.json',
    'weights/post_stats.safetensors', 'tokenizer/tokenizer.json',
])
def test_local_assets_must_exist(tmp_path, filename):
    path, *_ = checkpoint_fixture(tmp_path)
    (tmp_path / filename).unlink()
    with pytest.raises((ValueError, FileNotFoundError), match='tokenizer|safetensors|processor'): load(path)


def test_renamed_or_missing_camera_requires_an_explicit_matching_map(tmp_path):
    path, model, *_ = checkpoint_fixture(tmp_path)
    model['observations']['camera_map']['head'] = 'observation.images.other'
    write_json(path, model)
    with pytest.raises(ValueError, match='camera'): load(path)


def test_missing_normalizer_state_never_falls_back_to_identity(tmp_path):
    path, _, _, pre, _ = checkpoint_fixture(tmp_path)
    del pre['steps'][2]['state_file']
    write_json(tmp_path / 'weights/policy_preprocessor.json', pre)
    with pytest.raises(ValueError, match='normaliz'): load(path)


def test_relative_processor_cannot_be_combined_with_second_delta_conversion(tmp_path):
    path, model, cfg, pre, post = checkpoint_fixture(tmp_path)
    cfg['use_relative_actions'] = True
    pre['steps'].insert(2, {'registry_name': 'relative_actions_processor', 'config': {'enabled': True}})
    post['steps'].insert(1, {'registry_name': 'absolute_actions_processor', 'config': {'enabled': True}})
    model['actions']['representation'] = 'delta_joint'
    write_json(path, model)
    write_json(tmp_path / 'weights/config.json', cfg)
    write_json(tmp_path / 'weights/policy_preprocessor.json', pre)
    write_json(tmp_path / 'weights/policy_postprocessor.json', post)
    with pytest.raises(ValueError, match='relative|absolute'): load(path)


def test_coordinate_mapping_must_be_finite_and_invertible(tmp_path):
    path, model, *_ = checkpoint_fixture(tmp_path)
    model['observations']['state_scale'] = [1.0] * 13 + [0.0]
    write_json(path, model)
    with pytest.raises(ValueError, match='state_scale'): load(path)


def test_config_import_stays_gpu_framework_free():
    implementation()
    code = "import sys; import rc_pi05_policy.configuration; assert not {'torch','transformers','lerobot'} & sys.modules.keys()"
    subprocess.run([sys.executable, '-c', code], check=True,
                   env={**__import__('os').environ, 'PYTHONPATH': str(PACKAGE)})


def test_manifest_requires_the_existing_sync_qpos_boundary():
    validate = implementation().validate_manifest
    manifest = {'execution': {'mode': 'sync', 'control_mode': 'qpos', 'call_every_actions': 1},
                'params': {'model_config_path': 'model.json'}, 'hot_reloadable': []}
    validate(manifest)
    manifest['execution']['mode'] = 'async'
    with pytest.raises(ValueError, match='sync'): validate(manifest)


@pytest.mark.parametrize('value', [1e100, 1e-100])
def test_affine_scale_must_remain_finite_and_nonzero_in_float32(tmp_path, value):
    path, model, *_ = checkpoint_fixture(tmp_path)
    model['observations']['state_scale'] = [value] * 14
    write_json(path, model)
    with pytest.raises(ValueError, match='state_scale'): load(path)


@pytest.mark.parametrize('field,value', [('max_state_dim', 7), ('max_action_dim', 7), ('n_obs_steps', 2)])
def test_incompatible_model_tensor_layout_rejected_early(tmp_path, field, value):
    path, _, cfg, _, _ = checkpoint_fixture(tmp_path)
    cfg[field] = value
    write_json(tmp_path / 'weights/config.json', cfg)
    with pytest.raises(ValueError, match=field): load(path)


def test_relative_gripper_exclusions_require_saved_action_names(tmp_path):
    path, _, cfg, pre, post = checkpoint_fixture(tmp_path)
    cfg['use_relative_actions'] = True
    pre['steps'].insert(2, {'registry_name': 'relative_actions_processor',
                            'config': {'enabled': True, 'exclude_joints': ['gripper'], 'action_names': None}})
    post['steps'].insert(1, {'registry_name': 'absolute_actions_processor', 'config': {'enabled': True}})
    write_json(tmp_path / 'weights/config.json', cfg)
    write_json(tmp_path / 'weights/policy_preprocessor.json', pre)
    write_json(tmp_path / 'weights/policy_postprocessor.json', post)
    with pytest.raises(ValueError, match='action_names'): load(path)
