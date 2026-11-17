"""Reject a different training contract rather than emitting plausible wrong actions."""
import copy
import json

import numpy as np
import pytest

from pi05_helpers import write_json
from pi05_lift2_helpers import checkpoint_fixture
from rc_pi05_lift2.configuration import load_model_config


def test_dev_export_loads_its_own_lift2_statistics_without_saved_processors(tmp_path):
    path, _, _, _, _ = checkpoint_fixture(tmp_path, horizon=50, execute=40)
    config = load_model_config(path, project_root=tmp_path)
    assert config['execution']['chunk_size'] == 40
    assert config['execution']['request_when_remaining'] == 24
    assert config['actions']['representation'] == 'delta_joint'
    np.testing.assert_array_equal(config['statistics']['state']['mean'], np.ones(14))
    np.testing.assert_array_equal(config['statistics']['action']['mean'], np.full(14, 10.))
    assert config['model']['max_action_dim'] == 32


@pytest.mark.parametrize('mutation', ['reorder', 'quantiles', 'absolute', 'wrong_padding',
    'wrong_tokenizer', 'wrong_resize', 'extra_transform', 'custom_mapping', 'wrong_delta_mask'])
def test_different_training_pipeline_is_rejected(tmp_path, mutation):
    path, _, _, train, _ = checkpoint_fixture(tmp_path)
    dataset = train['dataset']
    steps = dataset['data_transforms']['inputs']
    if mutation == 'reorder': steps.insert(6, {'type': 'reorder_state_action'})
    elif mutation == 'quantiles': steps[2]['mode'] = 'quantiles'
    elif mutation == 'absolute': dataset['action_mode'] = 'abs'
    elif mutation == 'wrong_padding': steps[5]['max_action_dim'] = 16
    elif mutation == 'wrong_tokenizer': steps[4]['max_state_dim'] = 16
    elif mutation == 'wrong_resize': steps[1]['mode'] = 'nearest'
    elif mutation == 'extra_transform': dataset['model_transforms']['inputs'] = [{'type': 'custom'}]
    elif mutation == 'custom_mapping': steps[3]['mapping'] = {'action': ['other']}
    elif mutation == 'wrong_delta_mask': steps[0]['mask'] = [True] * 14
    write_json(tmp_path / 'weights/train_config.json', train)
    with pytest.raises(ValueError, match='train|transform|delta|pipeline'):
        load_model_config(path, project_root=tmp_path)


@pytest.mark.parametrize('mutation', ['missing_lift2', 'missing_field', 'wrong_width', 'negative_std', 'nonfinite'])
def test_bad_statistics_do_not_fall_back_to_another_robot(tmp_path, mutation):
    path, _, _, _, stats = checkpoint_fixture(tmp_path)
    data = {'arx_lift2': stats, 'arx_acone': copy.deepcopy(stats)}
    if mutation == 'missing_lift2': del data['arx_lift2']
    elif mutation == 'missing_field': del stats['actions.left_gripper.position']
    elif mutation == 'wrong_width': stats['states.left_joint.position']['mean'] = [1.]
    elif mutation == 'negative_std': stats['actions.right_gripper.position']['std'] = [-1.]
    elif mutation == 'nonfinite': stats['states.right_joint.position']['mean'][0] = float('inf')
    write_json(tmp_path / 'weights/stats.json', data)
    with pytest.raises((ValueError, KeyError), match='stat|finite|range|JSON|compliant'):
        load_model_config(path, project_root=tmp_path)


@pytest.mark.parametrize('mutation', ['shape', 'rtc', 'empty_camera', 'horizon', 'dtype', 'steps'])
def test_unsupported_model_contract_is_rejected(tmp_path, mutation):
    path, _, cfg, _, _ = checkpoint_fixture(tmp_path)
    if mutation == 'shape': cfg['output_features']['action']['shape'] = [16]
    elif mutation == 'rtc': cfg['rtc_config'] = {}
    elif mutation == 'empty_camera': cfg['empty_cameras'] = 1
    elif mutation == 'horizon': cfg['n_action_steps'] = cfg['chunk_size'] + 1
    elif mutation == 'dtype': cfg['dtype'] = 'float16'
    elif mutation == 'steps': cfg['num_inference_steps'] = 0
    write_json(tmp_path / 'weights/config.json', cfg)
    with pytest.raises(ValueError):
        load_model_config(path, project_root=tmp_path)


def test_affine_scale_must_remain_invertible_in_float32(tmp_path):
    path, raw, *_ = checkpoint_fixture(tmp_path)
    raw['observations']['state_scale'] = [1e-100] * 14
    write_json(path, raw)
    with pytest.raises(ValueError, match='invertibility|scale'):
        load_model_config(path, project_root=tmp_path)


def test_template_routes_dev_export_and_local_tokenizer(tmp_path):
    from pathlib import Path
    path, raw, *_ = checkpoint_fixture(tmp_path)
    template = json.loads((Path(__file__).resolve().parents[1]/'policy/pi05_lift2/model.json.example').read_text())
    template['model'].update(checkpoint=raw['model']['checkpoint'],tokenizer_path=raw['model']['tokenizer_path'])
    write_json(path,template)
    config = load_model_config(path,project_root=tmp_path)
    assert config['actions']['representation']=='delta_joint'
    assert config['observations']['camera_map']['right_wrist']=='observation.images.image2'


@pytest.mark.parametrize('mutation',['old_processors','absolute','swapped_camera','missing_file','hub_tokenizer'])
def test_upgrade_errors_are_rejected_before_model_loading(tmp_path, mutation):
    path, raw, *_ = checkpoint_fixture(tmp_path)
    if mutation == 'old_processors': raw['model']['preprocessor_filename']='policy_preprocessor.json'
    elif mutation == 'absolute': raw['actions']['representation']='absolute'
    elif mutation == 'swapped_camera':
        raw['observations']['camera_map']['head'],raw['observations']['camera_map']['left_wrist'] = (
            raw['observations']['camera_map']['left_wrist'],raw['observations']['camera_map']['head'])
    elif mutation == 'missing_file': (tmp_path/'weights/train_config.json').unlink()
    elif mutation == 'hub_tokenizer': raw['model']['tokenizer_path']='https://example.invalid/tokenizer'
    write_json(path,raw)
    with pytest.raises((ValueError,FileNotFoundError)):
        load_model_config(path,project_root=tmp_path)
