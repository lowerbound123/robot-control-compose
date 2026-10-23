import copy
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("model_client", reason="retired external HTTP model policy requires its separate model-client SDK")

ROOT=Path(__file__).resolve().parents[1]


def bridge_module():
    path=ROOT/'tests/fixtures/legacy_model_server/bridge.py'
    assert path.exists(), 'bridge policy implementation is missing'
    spec=importlib.util.spec_from_file_location('tested_bridge',path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


class Session:
    def __init__(self, chunks):
        self.chunks=list(chunks)
        self.calls=[]
        self.gate=threading.Event(); self.gate.set()
        self.entered=threading.Event()
        self.generation=1
        self.observe_enabled=True
        self.subtask_enabled=True
    def prepare(self,method,images,runtime): return method
    def request(self, method,state,images,runtime,params,prepared=None):
        self.calls.append((method,runtime.consumed_actions,runtime.observation_id,copy.deepcopy(params)))
        if method=='infer':
            self.entered.set(); assert self.gate.wait(3)
            return {'canonical_action_chunk':self.chunks.pop(0),'action_processor':'identity','subtask':''}
        return {'accepted':True}
    def reset(self): self.generation+=1
    def close(self): pass


def runtime(count, epoch=1):
    return SimpleNamespace(consumed_actions=count,observation_id=f'obs-{count}',control_epoch=epoch,config_version=1)


def settings(mode='sync'):
    return {'params':{'mode':mode,'request_when_remaining':2,
                     'fusion':{'trunk_mode':'close','trunk_smoothing':True,'max_smoothing_steps':5,
                               'exponential_factor':5.,'exponential_smoothing':.25},
                     'subtask':{'enabled':False,'refresh_every_actions':200,'minimum_lead_actions':20}}}


def test_sync_only_empty_requests_and_mirror_counts_real_actions():
    module=bridge_module()
    rows=np.arange(5,dtype=np.float32)[:,None]*np.ones((1,14),np.float32)
    session=Session([rows,rows+10])
    bridge=module.Bridge(settings(),runtime(0),session=session)
    state={'qpos':[0]*14}
    chunk=bridge.step(state,[],{},settings(),runtime(0))
    for count in range(1,5):
        chunk=bridge.step(state,chunk[1:],{},settings(),runtime(count))
        np.testing.assert_array_equal(chunk,rows[count:])
    assert len(session.calls)==1
    fresh=bridge.step(state,[],{},settings(),runtime(5))
    np.testing.assert_array_equal(fresh,rows+10)
    assert len(session.calls)==2
    bridge.close()


def test_async_background_trim_and_return_old_without_waiting():
    module=bridge_module()
    old=np.repeat(np.arange(6)[:,None],14,axis=1).astype(np.float32)
    new=old+3
    session=Session([old,new])
    config=settings('async-add')
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    session.gate.clear(); session.entered.clear()
    for count in range(1,5):
        chunk=bridge.step({'qpos':[0]*14},chunk[1:],{},config,runtime(count))
    assert session.entered.wait(1)
    before=time.monotonic()
    chunk=bridge.step({'qpos':[0]*14},chunk[1:],{},config,runtime(5))
    assert time.monotonic()-before<.05
    np.testing.assert_array_equal(chunk,old[5:])
    session.gate.set()
    bridge.pending.result(timeout=2)
    installed=bridge.step({'qpos':[0]*14},chunk,{},config,runtime(5))
    expected=module.TrajectoryAddTransition(**config['params']['fusion']).apply(old[5:],new[1:])
    np.testing.assert_array_equal(installed,expected)
    assert bridge.last_trim==1
    bridge.close()


@pytest.mark.parametrize('mode',['close','tail','now'])
@pytest.mark.parametrize('smooth',[False,True])
def test_fusion_matches_frozen_source_golden(mode,smooth):
    module=bridge_module()
    fixture=json.loads((ROOT/'tests/fixtures/async_add_golden.json').read_text())
    result=module.TrajectoryAddTransition(trunk_mode=mode,trunk_smoothing=smooth,
                                         max_smoothing_steps=5,exponential_factor=5.,exponential_smoothing=.25).apply(
                                             np.array(fixture['old'],np.float32),np.array(fixture['new'],np.float32))
    np.testing.assert_array_equal(result,np.array(fixture[f'{mode}:{smooth}'],np.float32))


def test_subtask_defers_when_too_near_infer_then_refreshes_after_install():
    module=bridge_module()
    rows=np.zeros((50,14),np.float32)
    session=Session([rows,rows])
    config=settings('async-add'); config['params']['request_when_remaining']=20
    config['params']['subtask']['enabled']=True
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    # A restored global counter must schedule relative to this run's baseline.
    bridge.last_refresh_consumed=-180
    chunk=bridge.step({'qpos':[0]*14},chunk[21:],{},config,runtime(21))
    assert not any(call[0]=='refresh' for call in session.calls)
    chunk=bridge.step({'qpos':[0]*14},chunk[9:],{},config,runtime(30))
    bridge.pending.result(2)
    chunk=bridge.step({'qpos':[0]*14},chunk,{},config,runtime(30))
    bridge.refresh_pending.result(2)
    assert any(call[0]=='refresh' for call in session.calls)
    bridge.reset()
    assert len(bridge.canonical)==0 and bridge.pending is None
    bridge.close()


@pytest.mark.parametrize('remaining,expected',[(39,False),(40,True),(41,True)])
def test_subtask_200_consumed_and_20_lead_boundary(remaining,expected):
    module=bridge_module()
    rows=np.zeros((remaining+200,14),np.float32)
    session=Session([rows])
    config=settings('async-add'); config['params']['request_when_remaining']=20
    config['params']['subtask']['enabled']=True
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    chunk=bridge.step({'qpos':[0]*14},chunk[199:],{},config,runtime(199))
    assert bridge.refresh_pending is None
    bridge.step({'qpos':[0]*14},chunk[1:],{},config,runtime(200))
    assert (bridge.refresh_pending is not None)==expected
    bridge.close()


def test_processor_runs_once_after_canonical_fusion():
    module=bridge_module()
    raw=np.full((6,14),-2.,np.float32)
    class GripperSession(Session):
        def request(self,*args,**kwargs):
            result=super().request(*args,**kwargs)
            if args[0]=='infer': result['action_processor']='qwen35_single_grippers'
            return result
    session=GripperSession([raw,raw])
    config=settings('async-add')
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    assert chunk[0,6]==np.float32(-1.85)
    for count in range(1,5):
        chunk=bridge.step({'qpos':[0]*14},chunk[1:],{},config,runtime(count))
        assert chunk[0,6]==np.float32(-1.85), 'old processed gripper must not accumulate offsets'
    bridge.pending.result(2)
    chunk=bridge.step({'qpos':[0]*14},chunk,{},config,runtime(4))
    assert chunk[0,6]==np.float32(-1.85)
    assert bridge.canonical[0,6]==-2.
    bridge.close()


def test_entirely_trimmed_result_requests_current_observation():
    module=bridge_module()
    rows=np.zeros((6,14),np.float32)
    session=Session([rows,rows[:1]+10,rows+20])
    config=settings('async-add')
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    chunk=bridge.step({'qpos':[0]*14},chunk[4:],{},config,runtime(4))
    bridge.pending.result(2)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(6))
    assert len(session.calls)==3
    assert session.calls[-1][2]=='obs-6'
    np.testing.assert_array_equal(chunk,rows+20)
    bridge.close()


def test_no_prefetch_refresh_is_acknowledged_before_next_action_inference():
    module=bridge_module()
    rows=np.zeros((6,14),np.float32)
    refresh_gate=threading.Event(); started=threading.Event()
    class DeferredSession(Session):
        def request(self,method,*args,**kwargs):
            if method=='refresh':
                started.set()
                assert refresh_gate.wait(3)
            return super().request(method,*args,**kwargs)
    session=DeferredSession([rows,rows])
    config=settings('async-add')
    config['params']['subtask'].update(enabled=True,prefetch=False,refresh_every_actions=1,minimum_lead_actions=1)
    bridge=module.Bridge(config,runtime(0),session=session)
    chunk=bridge.step({'qpos':[0]*14},[],{},config,runtime(0))
    chunk=bridge.step({'qpos':[0]*14},chunk[1:],{},config,runtime(1))
    assert started.wait(1)
    results=[]
    thread=threading.Thread(target=lambda:results.append(bridge.step({'qpos':[0]*14},chunk[3:],{},config,runtime(4))))
    try:
        thread.start();time.sleep(.03)
        assert len([c for c in session.calls if c[0]=='infer'])==1
        assert thread.is_alive(), 'next inference must wait for the dirty-cache acknowledgement'
    finally:
        refresh_gate.set();thread.join(2);bridge.close()
