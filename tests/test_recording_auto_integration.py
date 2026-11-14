"""Record Auto with real example workers, chains and connector lifecycles."""
from concurrent.futures import Future
import json
from pathlib import Path
import threading

import pytest

from test_example_controller import example_rc,command,ROOT
from test_controller import until
from test_connector_switch import profile
from project_templates import copy_templates


def rotated(rc,previous):
    until(lambda:rc.status()['recording']['state']=='recording'
        and rc.status()['recording']['session_id']!=previous
        and rc.status()['recording']['auto_pending']==0,8)
    return rc.status()['recording']['session_id']


def count(rc): return len(list((rc.outputs/'logs').glob('record_*')))


@pytest.mark.parametrize('chain',[False,True])
def test_example_auto_effect_matrix_and_active_exit(example_rc,chain):
    rc=example_rc
    if chain:
        copy_templates(ROOT/'policy/example_chain',rc.registry.directory/'example_chain')
        command(rc,'policy switch example_chain')
    target='example-1' if chain else 'example'
    initial=command(rc,'record auto on')['session_id']
    command(rc,f'policy {target} config set params.message ordinary')
    command(rc,f'policy {target} set-message ordinary-command')
    assert rc.status()['recording']['session_id']==initial and count(rc)==1
    for total,suffix in enumerate(('config set params.reset_token 1','config set params.chunk_size 5',
            'reinitialize','set-chunk-size 6','set-chunk-size-all 7'),2):
        before=rc.status()['recording']['session_id']
        command(rc,f'policy {target} {suffix}')
        rotated(rc,before)
        assert count(rc)==total
    until(lambda:rc.status()['recording']['samples_written']>=2)
    # Fixture exits while auto and the final recording remain active.


def test_chain_apply_cuts_once_but_chain_save_does_not(example_rc):
    rc=example_rc;first=command(rc,'record auto on')['session_id']
    command(rc,'take')
    receipt=rc.wait_operation(rc.submit_policy_chain(['example','example'],'save'),10)
    assert receipt['status']=='succeeded' and count(rc)==1
    receipt=rc.wait_operation(rc.submit_policy_chain(['example','example'],'apply'),10)
    assert receipt['status']=='succeeded'
    rotated(rc,first);assert count(rc)==2
    assert rc.control_owner=='mcp'


def test_failed_reinitialize_does_not_cut_successful_cold_retry_does(example_rc,monkeypatch):
    rc=example_rc;first=command(rc,'record auto on')['session_id']
    with monkeypatch.context() as patch:
        def fail(*args,**kwargs): raise RuntimeError('injected initialize failure')
        patch.setattr(rc._worker,'initialize_context',fail)
        command(rc,'policy example reinitialize',status='failed')
    assert rc.status()['recording']['session_id']==first and count(rc)==1
    command(rc,'policy example retry')
    rotated(rc,first);assert count(rc)==2


def test_connector_existing_boundary_once_and_idle_auto_restored(example_rc):
    rc=example_rc;first=command(rc,'record auto on')['session_id']
    command(rc,'connector switch '+profile(rc))
    rotated(rc,first);assert count(rc)==2
    command(rc,'record stop')
    second=rc.status()['recording']['session_id']
    command(rc,'connector switch '+profile(rc,'third',.3))
    rotated(rc,second);assert count(rc)==3
    assert json.loads((Path(rc.status()['recording']['path'])/'configs/initial.json').read_text())['connector']['profile']=='third'


def test_manual_stop_during_connector_switch_suppresses_old_auto_intent(example_rc,monkeypatch):
    rc=example_rc;command(rc,'record auto on')
    entered,release=threading.Event(),threading.Event()
    original=rc.robot.raw.close
    def close(**kwargs):
        entered.set();assert release.wait(8);original(**kwargs)
    monkeypatch.setattr(rc.robot.raw,'close',close)
    identity=rc.submit('connector switch '+profile(rc))
    try:
        assert entered.wait(5)
        command(rc,'record stop')
    finally: release.set()
    assert rc.wait_operation(identity,10)['status']=='succeeded'
    assert rc.status()['recording']['auto'] and rc.status()['recording']['state']=='idle'
    assert rc.status()['recording']['auto_pending']==0 and count(rc)==1
    command(rc,'reset')
    until(lambda:rc.status()['recording']['state']=='recording',5)
    assert count(rc)==2


def test_connector_waits_for_existing_auto_rollover_without_duplicate_cut(example_rc,monkeypatch):
    rc=example_rc;command(rc,'record auto on')
    entered,release=threading.Event(),threading.Event()
    original=rc._recorder.command
    def gate(action,*args,**kwargs):
        future=original(action,*args,**kwargs)
        if action!='off' or entered.is_set(): return future
        proxy=Future();entered.set()
        def relay():
            try:
                result=future.result(5)
                assert release.wait(8)
                proxy.set_result(result)
            except Exception as exc: proxy.set_exception(exc)
        thread=threading.Thread(target=relay,daemon=True);thread.start()
        return proxy
    monkeypatch.setattr(rc._recorder,'command',gate)
    command(rc,'reset');assert entered.wait(3)
    identity=rc.submit('connector switch '+profile(rc))
    try:
        until(lambda:rc.status()['connector']['phase'] not in ('ready',),3)
    finally: release.set()
    assert rc.wait_operation(identity,10)['status']=='succeeded'
    until(lambda:rc.status()['recording']['auto_pending']==0)
    assert count(rc)==3


def test_connector_candidate_cleanup_failure_releases_rotation_owner(example_rc,monkeypatch):
    from robot_control.connector import ConnectorRobot
    rc=example_rc;command(rc,'record auto on')
    original=rc._close_connector_candidate
    with monkeypatch.context() as patch:
        def connect(*args,**kwargs): raise RuntimeError('injected connection failure')
        def cleanup(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('injected candidate cleanup failure')
        patch.setattr(ConnectorRobot,'connect',connect)
        patch.setattr(rc,'_close_connector_candidate',cleanup)
        command(rc,'connector switch '+profile(rc),status='failed')
    assert not rc._record_transition_lock.locked()
    command(rc,'connector retry')
    assert rc.status()['recording']['state']=='recording' and count(rc)==2
