"""Runtime boundaries exercised against a stateful Engine double; live tests are separate."""
from config_helpers import read_config
import copy
import json
from pathlib import Path
import threading
import time

import pytest


class Engine:
    def __init__(self):
        self.containers = {}
        self.events = []
        self.unavailable = False
        self.late = False
        self.models = {}
    def __call__(self, args, *, timeout, env=None, cwd=None, cancel=None):
        if self.unavailable:
            raise RuntimeError('daemon unavailable')
        args = list(args)
        if args[:2] == ['info', '--format']:
            return 'daemon-test'
        if args[:2] == ['image', 'inspect']:
            return json.dumps([{'Id':'sha256:fixture'}])
        if args[0] == 'compose':
            file = Path(args[args.index('-f')+1])
            if 'config' in args:
                return file.read_text()
            model = read_config(file)
            if 'create' in args:
                cid = 'container-' + str(len(self.events))
                service = model['services']['policy']
                self.containers[cid] = {'Id':cid, 'Config':{'Labels':service['labels']},
                    'HostConfig':{'IpcMode':service['ipc']}, 'State':{'Running':False,'Dead':False,'Status':'created','ExitCode':0}}
                self.events.append(('create', cid))
                if self.late:
                    raise TimeoutError('CLI timed out after daemon created container')
                return cid
        if args[0] == 'ps':
            labels = [args[i+1][6:] for i,v in enumerate(args) if v == '--filter' and args[i+1].startswith('label=')]
            ids = [cid for cid,c in self.containers.items() if all(c['Config']['Labels'].get(k)==v for k,v in (x.split('=',1) for x in labels))]
            return '\n'.join(ids)
        if args[0] == 'inspect':
            return json.dumps([self.containers[x] for x in args[1:]])
        if args[0] in ('start','stop','kill','rm'):
            cid=args[-1]; self.events.append((args[0],cid))
            if args[0]=='rm': del self.containers[cid]
            elif args[0]=='start': self.containers[cid]['State']['Running']=True
            else: self.containers[cid]['State']['Running']=False
            return cid
        if args[:2] == ['network','ls']:
            return ''
        raise AssertionError(args)


def manager(tmp_path):
    from robot_control.runtime_manager import ComposeRuntimeManager
    engine = Engine()
    context = dict(deployment_id='deployment1', host_root=str(tmp_path), client_root=str(tmp_path),
                   base_project='base', rpc_volume='base-rpc', daemon_id='daemon-test', uid=1000, gid=1000)
    runtime = ComposeRuntimeManager(context, tmp_path/'runtime', run_id='run1', runner=engine)
    # A successful infrastructure query is injected; ownership and lifecycle use the Engine double.
    runtime.anchor_id = 'anchor1'
    runtime.check_infrastructure = lambda deadline, **kwargs: 'anchor1'
    return runtime, engine


def spec(runtime, tmp_path, identity):
    from robot_control.config import PolicyRecord
    folder=tmp_path/'policy'/identity; folder.mkdir(parents=True)
    (folder/'main.py').write_text('')
    (folder/'compose.yaml').write_text(json.dumps({'services':{'policy':{'image':'fixture:local'}}}))
    record=PolicyRecord(folder, dict(schema_version=3,id=identity,params={},execution=dict(mode='sync',call_every_actions=1,control_mode='qpos')))
    return runtime.prepare_spec(record, deadline=time.monotonic()+10)


def test_handles_cleanup_without_compose_file_and_distinguish_ids(tmp_path):
    runtime, engine=manager(tmp_path)
    a=spec(runtime,tmp_path,'a_b'); b=spec(runtime,tmp_path,'a-b')
    first=runtime.start(a,1,threading.Event(),time.monotonic()+10)
    second=runtime.start(b,1,threading.Event(),time.monotonic()+10)
    assert first.project != second.project and first.container_id != second.container_id
    (tmp_path/'policy'/'a_b'/'compose.yaml').unlink()
    proof=runtime.stop_and_remove(first,time.monotonic()+10)
    assert proof.container_id==first.container_id and proof.removed
    assert second.container_id in engine.containers


def test_daemon_failure_is_not_exit_proof(tmp_path):
    runtime,engine=manager(tmp_path)
    handle=runtime.start(spec(runtime,tmp_path,'a'),1,threading.Event(),time.monotonic()+10)
    engine.unavailable=True
    with pytest.raises(Exception,match='unavailable'):
        runtime.stop_and_remove(handle,time.monotonic()+10)
    assert handle.container_id in engine.containers


def test_timeout_intent_finds_and_cleans_daemon_created_container(tmp_path):
    runtime,engine=manager(tmp_path); engine.late=True
    with pytest.raises(TimeoutError):
        runtime.start(spec(runtime,tmp_path,'a'),2,threading.Event(),time.monotonic()+10)
    handles=runtime.reconcile_owned(time.monotonic()+10)
    assert len(handles)==1
    runtime.stop_and_remove(handles[0],time.monotonic()+10)
    assert not engine.containers


def test_tampered_labels_never_authorize_removal(tmp_path):
    runtime,engine=manager(tmp_path)
    handle=runtime.start(spec(runtime,tmp_path,'a'),1,threading.Event(),time.monotonic()+10)
    engine.containers[handle.container_id]['Config']['Labels']['io.robot-control.deployment']='someone-else'
    with pytest.raises(RuntimeError,match='ownership'):
        runtime.stop_and_remove(handle,time.monotonic()+10)
    assert not any(e[0] in ('stop','rm','kill') for e in engine.events)


def test_changed_daemon_cannot_prove_old_container_absent(tmp_path):
    runtime,engine=manager(tmp_path)
    handle=runtime.start(spec(runtime,tmp_path,'a'),1,threading.Event(),time.monotonic()+10)
    original=runtime.runner
    def restarted(args,**kwargs):
        if args[:2]==['info','--format']:return 'different-daemon'
        if args[0]=='ps':return ''
        return original(args,**kwargs)
    runtime.runner=restarted
    with pytest.raises(RuntimeError,match='daemon'):
        runtime.stop_and_remove(handle,time.monotonic()+10)
