"""Task commits fence old chunks while weights and owned model threads survive."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from config_helpers import read_config
from pi05_helpers import checkpoint_fixture as native_fixture
from pi05_lift2_helpers import checkpoint_fixture as dev_fixture
from test_qwen35_runtime import Model, images

ROOT = Path(__file__).resolve().parents[1]


class TaskModel(Model):
    def predict(self,qpos,frames,instruction):
        output = super().predict(qpos,frames,instruction)
        if instruction == getattr(self,'fail_task',None):
            raise RuntimeError('old task inference failed')
        output[:,0] = 2. if instruction == 'task B' else 1.
        return output


@pytest.fixture(params=['pi05','pi05_lift2'])
def running(request,tmp_path):
    source=request.param
    spec=importlib.util.spec_from_file_location('task_'+source,ROOT/'policy'/source/'main.py')
    entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
    manifest=read_config(ROOT/'policy'/source/'policy.yaml.example')
    path,*_= (native_fixture if source=='pi05' else dev_fixture)(tmp_path)
    cfg=entry.load_model_config(path,project_root=tmp_path)
    model=TaskModel()
    policy=entry.AsyncPi05Policy(cfg,policy_config=manifest,backend=model,project_root=tmp_path)
    try: yield source,entry,manifest,policy,model
    finally:
        model.gate.set();policy.close()


def step(policy,chunk,count,epoch=1,version=1):
    return policy.step({'qpos':np.full(14,count)},chunk,images(),runtime=SimpleNamespace(
        consumed_actions=count,control_epoch=epoch,config_version=version))


def test_task_command_validates_without_mutating_running_context(running):
    _,entry,manifest,policy,model=running
    before=copy.deepcopy(manifest)
    assert entry.switch_task(policy,'task B')=={'params.task':'task B'}
    assert manifest==before and policy.instruction=='pick up the object'
    for bad in ('','  ',None,[],True):
        with pytest.raises(ValueError,match='task'):entry.switch_task(policy,bad)
    assert manifest==before and len(model.calls)==0


@pytest.mark.parametrize('old_failure',[False,True])
def test_pending_old_task_cannot_blend_into_new_task(running,old_failure):
    _,entry,manifest,policy,model=running
    chunk=step(policy,[],0)
    model.started.clear();model.gate.clear()
    chunk=step(policy,chunk[21:],21)
    assert model.started.wait(1)
    if old_failure:model.fail_task='pick up the object'
    manifest['params'].update(task=entry.switch_task(policy,'task B')['params.task'])
    with ThreadPoolExecutor(1) as executor:
        pending=executor.submit(step,policy,[],22,2,2)
        try: assert not pending.done()
        finally:model.gate.set()
        fresh=pending.result(3)
    np.testing.assert_array_equal(fresh[:,0],2.)
    assert model.calls[-1][2]=='task B'
    np.testing.assert_array_equal(model.calls[-1][0],22.)
    assert sum(name=='load' for name,_ in model.events)==1
    assert sum(name=='reset' for name,_ in model.events)==1
    np.testing.assert_array_equal(step(policy,fresh[1:],23,2,2),fresh[1:])


def test_configure_task_reset_and_null_fallback_keep_loaded_model(running):
    _,entry,manifest,policy,model=running
    step(policy,[],0)
    actions=[]
    entry.configure(policy,manifest,'params.task','"task B"',actions.append)
    assert actions==['reset']
    policy.reset()  # SDK calls reset only after committing the candidate.
    np.testing.assert_array_equal(step(policy,[],1,2,2)[:,0],2.)
    entry.configure(policy,manifest,'params.task','null',actions.append)
    policy.reset()
    np.testing.assert_array_equal(step(policy,[],2,3,3)[:,0],1.)
    assert model.calls[-1][2]=='pick up the object'
    assert sum(name=='load' for name,_ in model.events)==1


@pytest.mark.parametrize('raw',['""','"  "','true','12','[]','{}'])
def test_invalid_task_config_has_no_side_effects(running,raw):
    _,entry,manifest,policy,_=running
    before=copy.deepcopy(manifest)
    with pytest.raises(ValueError,match='task'):
        entry.configure(policy,manifest,'params.task',raw,lambda _:pytest.fail('no callback'))
    assert manifest==before


def test_pi05_defaults_preserve_selected_robot_gripper_rules(running):
    source,_,_,policy,model=running
    model.output[:,:6]=-.25
    model.output[:,6]=np.tile([-1.,0.,2.5,5.,6.],10)
    model.output[:,13]=np.tile([6.,5.,2.5,0.,-1.],10)
    original=model.output.copy()
    output=step(policy,[],0)
    np.testing.assert_allclose(output[:,6],np.tile([0.,0.,2.5,5.,5.] if source=='pi05' else [0.,0.,4.9,4.9,4.9],10))
    np.testing.assert_allclose(output[:,13],np.tile([5.,5.,2.5,0.,0.] if source=='pi05' else [4.9,4.9,4.9,0.,0.],10))
    np.testing.assert_array_equal(output[:,1:6],-.25)
    np.testing.assert_array_equal(model.output,original)


@pytest.mark.parametrize('source',['pi05','pi05_lift2'])
def test_setup_upgrades_existing_hot_whitelist_and_preserves_signed_values(tmp_path,source):
    import json
    from config_files import configuration_updates
    folder=tmp_path/'policy'/source;folder.mkdir(parents=True)
    template=read_config(ROOT/'policy'/source/'policy.yaml.example')
    rules={'gripper_upper_threshold':5.,'gripper_upper_value':5.,
           'gripper_lower_threshold':0.,'gripper_lower_value':0.}
    template['params'].update(task=None,**rules)
    template['hot_reloadable']=['params.task',*('params.'+k for k in rules)]
    old=copy.deepcopy(template)
    old['execution']['on_connector_switch']='reinitialize_policy'
    old['hot_reloadable']=['execution.on_connector_switch']
    old['params'].update(task='saved task',gripper_upper_value='+0.5',gripper_lower_value='-0.25')
    (folder/'policy.yaml.example').write_text(json.dumps(template))
    (folder/'policy.yaml').write_text(json.dumps(old))
    changes,documents=configuration_updates(tmp_path)
    new=documents[f'policy/{source}/policy.yaml']
    assert new['params']==old['params']
    assert set(template['hot_reloadable'])<=set(new['hot_reloadable'])
    assert len(changes)==1
    changes[0][0].write_bytes(changes[0][1])
    assert configuration_updates(tmp_path)[0]==[]


@pytest.mark.parametrize('source',['pi05','pi05_lift2'])
def test_setup_installs_task_command_into_pre_feature_manifests(tmp_path,source):
    import json
    from config_files import configuration_updates
    folder=tmp_path/'policy'/source;folder.mkdir(parents=True)
    template=read_config(ROOT/'policy'/source/'policy.yaml.example')
    previous=copy.deepcopy(template)
    previous['params'].pop('task')
    previous['tui_commands'].pop('switch-task')
    previous['config_hints']['settings'].pop('params.task')
    previous['hot_reloadable'].remove('params.task')
    if source=='pi05':
        for key in list(previous['params']):
            if key.startswith('gripper_'):
                previous['params'].pop(key)
                previous['hot_reloadable'].remove('params.'+key)
                previous['config_hints']['settings'].pop('params.'+key)
    previous['params']['model_config_path']='site-model.json'
    (folder/'policy.yaml.example').write_text(json.dumps(template))
    (folder/'policy.yaml').write_text(json.dumps(previous))
    changes,documents=configuration_updates(tmp_path)
    updated=documents[f'policy/{source}/policy.yaml']
    assert updated['params']['task'] is None
    assert updated['params']['model_config_path']=='site-model.json'
    assert updated['tui_commands']['switch-task']['effect']=='update_config'
    assert 'params.task' in updated['hot_reloadable']
    assert updated['params']['gripper_upper_value']==(5. if source=='pi05' else 4.9)
    assert len(changes)==1
