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


def test_repeated_instances_keep_source_mount_and_image_but_get_distinct_ownership(tmp_path):
    from types import SimpleNamespace
    runtime, engine = manager(tmp_path)
    original = spec(runtime, tmp_path, 'a')
    config = json.loads(original.config_json)
    records = [SimpleNamespace(directory=Path(original.directory), config=config, version=1,
                               source_id='a', instance_id=identity,
                               runtime_config={**config, 'id': identity}) for identity in ('a-0', 'a-1')]
    specs = [runtime.prepare_spec(record, deadline=time.monotonic() + 10) for record in records]
    assert [item.policy_id for item in specs] == ['a-0', 'a-1']
    assert [item.socket for item in specs] == ['/run/robot-control/a-0.sock', '/run/robot-control/a-1.sock']
    for item in specs:
        service = json.loads(item.model_json)['services']['policy']
        assert service['image'] == 'sha256:fixture'
        assert service['command'] == ['--host-id', item.policy_id, '--source-policy-id', 'a',
                                      '--socket', item.socket, '--policy-root', '/workspace/policy']
        mounts = [mount for mount in service['volumes'] if mount['target'].startswith('/workspace/policy/')]
        assert len(mounts) == 1 and mounts[0]['target'] == '/workspace/policy/a'
        assert mounts[0]['source'] == original.directory and mounts[0]['read_only']
        assert json.loads(item.config_json)['id'] == item.policy_id
    handles = [runtime.start(item, 7, threading.Event(), time.monotonic() + 10) for item in specs]
    assert handles[0].project != handles[1].project
    assert handles[0].container_id != handles[1].container_id
    proof = runtime.stop_and_remove(handles[0], time.monotonic() + 10)
    assert handles[1].container_id in engine.containers
    from robot_control.process import PolicyProcess
    first = PolicyProcess(records[0].directory, records[0].runtime_config,
                          endpoint=handles[0], exit_authority=runtime.authority)
    second = PolicyProcess(records[1].directory, records[1].runtime_config,
                           endpoint=handles[1], exit_authority=runtime.authority)
    with pytest.raises(ValueError, match='untrusted|stale'):
        second.accept_exit_proof(proof)
    assert not second.cleanup_confirmed
    first.accept_exit_proof(proof)
    assert first.cleanup_confirmed


def test_occurrence_network_names_are_project_scoped_and_external_networks_unchanged(tmp_path):
    from types import SimpleNamespace
    runtime, engine = manager(tmp_path)
    original = spec(runtime, tmp_path, 'a')
    folder = Path(original.directory)
    networks = {'default': {'name': 'a_default', 'driver': 'bridge'},
                'private': {'name': 'user-private', 'internal': True, 'labels': {'purpose': 'policy'}},
                'external': {'name': 'shared-model-network', 'external': True}}
    (folder / 'compose.yaml').write_text(json.dumps({'services': {'policy': {
        'image': 'fixture:local', 'networks': {'default': {}, 'private': {}, 'external': {}}}},
        'networks': networks}))
    config = json.loads(original.config_json)
    handles = []
    for identity in ('a-0', 'a-1'):
        record = SimpleNamespace(directory=folder, config=config, version=1, source_id='a',
                                 instance_id=identity, runtime_config={**config, 'id': identity})
        prepared = runtime.prepare_spec(record, deadline=time.monotonic() + 10)
        handle = runtime.start(prepared, 2, threading.Event(), time.monotonic() + 10)
        handles.append(handle)
        snapshot = json.loads((runtime.state_dir / (handle.project + '.compose.json')).read_text())
        assert snapshot['networks']['default'] == {'name': handle.project + '_default', 'driver': 'bridge'}
        assert snapshot['networks']['private'] == {'name': handle.project + '_private', 'internal': True,
                                                   'labels': {'purpose': 'policy'}}
        assert snapshot['networks']['external'] == {'name': 'shared-model-network', 'external': True}
        assert json.loads(prepared.model_json)['networks'] == networks
    assert handles[0].project != handles[1].project
