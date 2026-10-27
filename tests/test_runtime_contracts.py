import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_host_only_accepts_assigned_directory_and_hello_binds_generation(tmp_path,monkeypatch):
    from rc_policy_sdk.host import PolicyHost
    for name in ('a','b'):
        folder=tmp_path/name;folder.mkdir()
        (folder/'main.py').write_text('\n'.join(f'def {x}(*args): pass' for x in ('initialize','policy','reset_context','release')))
    monkeypatch.setenv('RC_RUN_ID','run1');monkeypatch.setenv('RC_GENERATION','7')
    host=PolicyHost(tmp_path,'a')
    try:
        assert host.preflight('a')['valid']
        with pytest.raises(ValueError,match='assigned'):
            host.preflight('b')
        info=host.dispatch(SimpleNamespace(owner=None),dict(type='hello',owner_id='owner',protocol_version=1,image_protocol_version=1))
        assert info['run_id']=='run1' and info['generation']==7
        host.cleanup_failed=True
        assert host.dispatch(SimpleNamespace(owner=None),dict(type='hello',owner_id='owner',protocol_version=1,image_protocol_version=1))['healthy'] is False
    finally:host.shutting_down=True


def test_chain_duplicate_bare_commands_are_not_routed_to_last_member():
    from robot_control.chain_config import chain_commands
    spec={'handler':'change','args':[]}
    members=[SimpleNamespace(config={'id':x,'tui_commands':{'change':spec}}) for x in ('a','b')]
    commands,targets=chain_commands(members)
    assert 'change' not in commands
    assert targets=={'a-change':('a','change'),'b-change':('b','change')}


def test_sdk_cleanup_does_not_confirm_live_root_even_without_descendants(monkeypatch):
    import rc_policy_sdk.supervisor as supervisor
    monkeypatch.setattr(supervisor,'descendant_snapshot',lambda *_:{})
    monkeypatch.setattr(supervisor,'_reap',lambda *_:None)
    clock=iter([0,10,20]); monkeypatch.setattr(supervisor.time,'monotonic',lambda:next(clock))
    assert supervisor._cleanup(SimpleNamespace(poll=lambda:None,pid=123)) is False


def test_cross_run_external_recovery_requires_matching_instance(tmp_path,monkeypatch):
    from robot_control.external import ExternalResources
    record=dict(remote_id='r',owner_id='owner-old',instance_id='instance-old',endpoint='http://127.0.0.1:1',model_id='model',requests={})
    journal=tmp_path/'old.json';journal.write_text(json.dumps({'version':3,'deployment_id':'dep','remote_models':[record]}))
    monkeypatch.setattr(ExternalResources,'_http',staticmethod(lambda *a,**k:{'instance_id':'instance-new'}))
    with pytest.raises(RuntimeError,match='instance'):
        ExternalResources.recover(tmp_path,time.monotonic()+1,'dep')
    assert journal.exists()


def test_cross_run_external_owner_is_revoked_before_journal_removed(tmp_path,monkeypatch):
    from robot_control.external import ExternalResources
    record=dict(remote_id='r',owner_id='old-owner',instance_id='inst',endpoint='http://127.0.0.1:1',model_id='m',requests={})
    journal=tmp_path/'old.json';journal.write_text(json.dumps({'version':3,'deployment_id':'dep','remote_models':[record]}))
    calls=[]
    def http(record,path,body=None,timeout=1):
        calls.append((path,body))
        if path=='/v1/status':return {'instance_id':'inst'}
        if path=='/v1/models/unload':return {'operation_id':'op'}
        return {'status':'succeeded','result':{'instance_id':'inst','state':'unloaded','worker_pid':None}}
    monkeypatch.setattr(ExternalResources,'_http',staticmethod(http))
    ExternalResources.recover(tmp_path,time.monotonic()+1,'dep')
    assert calls[1][1]=={'owner_id':'old-owner','expected_instance_id':'inst'}
    assert not journal.exists()
