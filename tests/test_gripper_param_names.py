"""Canonical gripper names and value-preserving migration of private manifests."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from config_helpers import read_config
from robot_control.yaml_config import render_yaml_config, load_yaml_config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
RENAMES = {
    'gripper_threshold':'gripper_upper_threshold',
    'gripper_high_value':'gripper_upper_value',
    'gripper_low_threshold':'gripper_lower_threshold',
    'gripper_low_value':'gripper_lower_value',
}
SOURCES = ('qwen35_history','qwen35_history_lift2','pi05','pi05_lift2')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('old,new', RENAMES.items())
def test_old_params_cannot_silently_fall_back_to_defaults(source, old, new):
    folder = ROOT/'policy'/source
    spec = importlib.util.spec_from_file_location('rename_'+source,folder/'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = read_config(folder/'policy.yaml.example')
    config['hot_reloadable'] = []
    config['params'].pop(new,None)
    config['params'][old] = 3.125
    with pytest.raises(ValueError,match='unsupported gripper'):
        module.validate_config(config)
    before = copy.deepcopy(config)
    with pytest.raises(ValueError,match='unsupported configuration key'):
        module.configure(None,config,'params.'+old,'2.25',lambda _:pytest.fail('no lifecycle'))
    assert config == before


def legacy_config():
    return {'schema_version':3,'id':'qwen35_history','execution':{'control_mode':'qpos'},
            'params':{'model_config_path':'custom.json','task':'literal gripper_threshold',
                      'gripper_threshold':3.125,'gripper_high_value':4.25,
                      'gripper_low_threshold':None,'gripper_low_value':.75},
            'hot_reloadable':['params.task','params.gripper_threshold','params.gripper_low_value'],
            'config_hints':{'help':'设置 params.gripper_threshold', 'settings':{
                'params.gripper_threshold':{'type':'float','help':'输出 gripper_high_value'},
                'params.task':{'type':'str','choices':['gripper_threshold']}}}}


def test_migration_renames_only_fields_and_metadata_and_is_idempotent():
    from migrate_gripper_params import migrate_config
    before = legacy_config()
    saved = copy.deepcopy(before)
    actual = migrate_config(before)
    assert actual['params'] == {'model_config_path':'custom.json','task':'literal gripper_threshold',
        'gripper_upper_threshold':3.125,'gripper_upper_value':4.25,
        'gripper_lower_threshold':None,'gripper_lower_value':.75}
    assert actual['hot_reloadable'] == ['params.task','params.gripper_upper_threshold','params.gripper_lower_value']
    assert actual['config_hints']['settings']['params.gripper_upper_threshold']['help'] == '输出 gripper_upper_value'
    assert actual['config_hints']['settings']['params.task']['choices'] == ['gripper_threshold']
    assert actual['config_hints']['help'] == '设置 params.gripper_upper_threshold'
    assert before == saved and migrate_config(actual) == actual


def test_equal_duplicate_names_merge_but_conflicting_values_are_rejected():
    from migrate_gripper_params import migrate_config
    current = legacy_config()
    current['params']['gripper_upper_threshold'] = 3.125
    current['hot_reloadable'].append('params.gripper_upper_threshold')
    migrated = migrate_config(current)
    assert migrated['params']['gripper_upper_threshold'] == 3.125
    assert migrated['hot_reloadable'].count('params.gripper_upper_threshold') == 1
    current['params']['gripper_upper_threshold'] = 1.5
    saved = copy.deepcopy(current)
    with pytest.raises(ValueError,match='conflicting'):
        migrate_config(current)
    assert current == saved


def test_offline_migration_dry_run_backup_and_conflict_do_not_overwrite_values(tmp_path):
    from migrate_gripper_params import main
    first = tmp_path/'policy/qwen35_history/policy.yaml'
    first.parent.mkdir(parents=True)
    original = render_yaml_config(first,legacy_config())
    first.write_bytes(original)
    assert main([],root=tmp_path) == 0 and first.read_bytes()==original
    assert not (tmp_path/'.setup').exists()
    second = tmp_path/'policy/qwen35_history_lift2/policy.yaml'
    second.parent.mkdir(parents=True)
    bad = legacy_config()
    bad['params']['gripper_upper_threshold'] = 1.5
    second.write_bytes(render_yaml_config(second,bad))
    assert main(['--apply'],root=tmp_path)==1
    assert first.read_bytes()==original and not (tmp_path/'.setup').exists()
    second.unlink()
    assert main(['--apply'],root=tmp_path)==0
    assert load_yaml_config(first)['params']['gripper_upper_threshold']==3.125
    backups = list((tmp_path/'.setup/backups').glob('*/policy/qwen35_history/policy.yaml'))
    assert len(backups)==1 and backups[0].read_bytes()==original
    assert main(['--apply'],root=tmp_path)==0
    assert len(list((tmp_path/'.setup/backups').iterdir()))==1


@pytest.mark.parametrize('source', SOURCES)
def test_setup_migrates_values_before_merging_new_template_defaults(tmp_path, source):
    from config_files import configuration_updates
    target = tmp_path/'policy'/source/'policy.yaml'
    target.parent.mkdir(parents=True)
    template = read_config(ROOT/'policy'/source/'policy.yaml.example')
    example = target.with_name('policy.yaml.example')
    example.write_bytes(render_yaml_config(example,template))
    old = copy.deepcopy(template)
    old['params'] = {key:value for key,value in old['params'].items() if not key.startswith('gripper_')}
    old['params']['gripper_threshold'] = 7.125
    old['hot_reloadable'] = ['params.gripper_threshold']
    target.write_bytes(render_yaml_config(target,old))
    changes, documents = configuration_updates(tmp_path)
    prepared = documents[f'policy/{source}/policy.yaml']
    assert prepared['params']['gripper_upper_threshold']==7.125
    assert 'gripper_threshold' not in prepared['params']
    assert 'params.gripper_upper_threshold' in prepared['hot_reloadable']
    assert any(path==target for path,_ in changes)
    assert load_yaml_config(target)['params']['gripper_threshold']==7.125


@pytest.mark.parametrize('source', SOURCES)
def test_setup_and_legacy_name_migration_preserve_delta_strings(tmp_path, source):
    from config_files import configuration_updates, write_updates
    target = tmp_path/'policy'/source/'policy.yaml'
    target.parent.mkdir(parents=True)
    example = target.with_name('policy.yaml.example')
    example.write_bytes((ROOT/'policy'/source/'policy.yaml.example').read_bytes())
    old = read_config(example)
    old['params']['gripper_high_value'] = '+0.5'
    old['params'].pop('gripper_upper_value')
    old['params']['gripper_lower_value'] = '-5e-1'
    target.write_bytes(render_yaml_config(target,old))
    original = target.read_bytes()
    changes, documents = configuration_updates(tmp_path)
    params = documents[f'policy/{source}/policy.yaml']['params']
    assert params['gripper_upper_value'] == '+0.5' and params['gripper_lower_value'] == '-5e-1'
    write_updates(tmp_path,changes)
    assert load_yaml_config(target)['params'] == params
    assert next((tmp_path/'.setup/backups').glob('*/policy/*/policy.yaml')).read_bytes() == original
    assert configuration_updates(tmp_path)[0] == []


@pytest.mark.parametrize('source,field', [('qwen35_history','gripper_upper_threshold'),
    ('hold','gripper_upper_value'), ('example','gripper_lower_value')])
def test_setup_delta_union_does_not_weaken_other_field_or_source_types(tmp_path, source, field):
    from config_files import configuration_updates
    target = tmp_path/'policy'/source/'policy.yaml'
    target.parent.mkdir(parents=True)
    template = {'params': {field:0.5}}
    example = target.with_name('policy.yaml.example')
    example.write_bytes(render_yaml_config(example,template))
    target.write_bytes(render_yaml_config(target,{'params':{field:'+0.5'}}))
    with pytest.raises(ValueError,match='type conflict'):
        configuration_updates(tmp_path)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('value',['+NaN','-Infinity','+1e100','1.5','+1+2'])
def test_setup_rejects_invalid_delta_before_preparing_any_writes(tmp_path, source, value):
    from config_files import configuration_updates
    target=tmp_path/'policy'/source/'policy.yaml'
    target.parent.mkdir(parents=True)
    example=target.with_name('policy.yaml.example')
    example.write_bytes((ROOT/'policy'/source/'policy.yaml.example').read_bytes())
    current=read_config(example)
    current['params']['gripper_upper_value']=value
    target.write_bytes(render_yaml_config(target,current))
    original=target.read_bytes()
    with pytest.raises(ValueError,match='gripper_upper_value'):
        configuration_updates(tmp_path)
    assert target.read_bytes()==original and not (tmp_path/'.setup').exists()


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('value',[1.25,'+0.5'])
def test_setup_refreshes_legacy_float_hints_without_changing_y_values(tmp_path, source, value):
    from config_files import configuration_updates, write_updates
    target=tmp_path/'policy'/source/'policy.yaml'
    target.parent.mkdir(parents=True)
    example=target.with_name('policy.yaml.example')
    example.write_bytes((ROOT/'policy'/source/'policy.yaml.example').read_bytes())
    current=read_config(example)
    current['params']['gripper_upper_value']=value
    for name in ('gripper_upper_value','gripper_lower_value'):
        current['config_hints']['settings']['params.'+name]={'type':'float','help':'legacy assignment only'}
    target.write_bytes(render_yaml_config(target,current))
    changes,documents=configuration_updates(tmp_path)
    merged=documents[f'policy/{source}/policy.yaml']
    assert merged['params']==current['params']
    for name in ('gripper_upper_value','gripper_lower_value'):
        assert merged['config_hints']['settings']['params.'+name] == read_config(example)['config_hints']['settings']['params.'+name]
    write_updates(tmp_path,changes)
    assert configuration_updates(tmp_path)[0]==[]
