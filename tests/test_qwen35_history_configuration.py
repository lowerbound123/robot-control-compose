import importlib
import json
import sys

import pytest
from history_helpers import ROOT, model_file

sys.path.insert(0, str(ROOT / 'policy/qwen35_history/src'))


def implementation():
    assert (ROOT / 'policy/qwen35_history/src/rc_qwen35_history/configuration.py').is_file(), 'configuration missing'
    return importlib.import_module('rc_qwen35_history.configuration')


@pytest.mark.parametrize('x', [1, 25, 50])
def test_resolves_checkpoint_and_preserves_cold_cadence(tmp_path, x):
    path = model_file(tmp_path, x=x)
    config = implementation().load_model_config('model.json', project_root=tmp_path)
    assert config['model']['checkpoint'] == str(tmp_path / 'weights')
    assert config['execution']['replan_every_actions'] == x
    assert config['inference']['use_cuda_graph'] is True
    assert config['model']['vlm_model_name_or_path'] == 'Qwen/Qwen3.5-4B'


@pytest.mark.parametrize('x', [0, -1, 51, True, 1.5, '25'])
def test_rejects_bad_x_before_loading_model(tmp_path, x):
    model_file(tmp_path, x=x)
    with pytest.raises(ValueError, match='replan_every_actions'):
        implementation().load_model_config('model.json', project_root=tmp_path)


@pytest.mark.parametrize('file,mutate', [
    ('config', lambda d: d.update(type='qwen3_5vla_ki_wan')),
    ('config', lambda d: d.update(max_action_dim=13)),
    ('config', lambda d: d.update(context_slot_stride=0)),
    ('train_config', lambda d: d['dataset'].update(norm_mode='unknown')),
    ('train_config', lambda d: d['dataset'].update(action_mode='eef')),
    ('stats', lambda d: d['arx_acone']['actions.left_joint.position'].update(std=[-1.]*6)),
])
def test_rejects_incompatible_checkpoint_metadata(tmp_path, file, mutate):
    model_file(tmp_path)
    path = tmp_path / 'weights' / f'{file}.json'
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        implementation().load_model_config('model.json', project_root=tmp_path)


def test_manifest_uses_existing_contract_and_only_task_is_hot():
    from robot_control.yaml_config import load_yaml_config
    path = ROOT / 'policy/qwen35_history/policy.yaml.example'
    assert path.exists(), 'history manifest missing'
    config = load_yaml_config(path)
    implementation().validate_manifest(config)
    for field, value in [('mode', 'async'), ('call_every_actions', 25), ('control_mode', 'epos')]:
        changed = json.loads(json.dumps(config))
        changed['execution'][field] = value
        with pytest.raises(ValueError):
            implementation().validate_manifest(changed)
    config['hot_reloadable'].append('params.model_config_path')
    with pytest.raises(ValueError):
        implementation().validate_manifest(config)


@pytest.mark.parametrize('package', ['rc_qwen35_history', 'rc_qwen35_history_lift2'])
@pytest.mark.parametrize('fields', [[], ['Memory', 'Scene', 'Subtask'], ['Subtask'], ['Scene', 'Memory']])
def test_language_generation_fields_are_explicit_and_canonical(tmp_path, package, fields):
    path = model_file(tmp_path)
    data = json.loads(path.read_text())
    data['inference'] = {'generate_language': fields}
    path.write_text(json.dumps(data))
    module = importlib.import_module(package + '.configuration')
    config = module.load_model_config(path, project_root=tmp_path)
    assert config['inference']['generate_language'] == [name for name in ('Memory', 'Scene', 'Subtask') if name in fields]


@pytest.mark.parametrize('package', ['rc_qwen35_history', 'rc_qwen35_history_lift2'])
@pytest.mark.parametrize('fields', [True, None, 'Subtask', ['Other'], ['Subtask', 'Subtask'], [1], [{}]])
def test_invalid_language_generation_is_rejected_before_model_load(tmp_path, package, fields):
    path = model_file(tmp_path)
    data = json.loads(path.read_text())
    data['inference'] = {'generate_language': fields}
    path.write_text(json.dumps(data))
    module = importlib.import_module(package + '.configuration')
    with pytest.raises(ValueError, match='generate_language'):
        module.load_model_config(path, project_root=tmp_path)


@pytest.mark.parametrize('package', ['rc_qwen35_history', 'rc_qwen35_history_lift2'])
def test_legacy_model_config_keeps_language_generation_disabled(tmp_path, package):
    path = model_file(tmp_path)
    module = importlib.import_module(package + '.configuration')
    assert module.load_model_config(path, project_root=tmp_path)['inference']['generate_language'] == []
