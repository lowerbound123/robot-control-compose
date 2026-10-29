import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))


def test_policy_rc_named_business_document_is_not_a_controller_config():
    from config_validation import validate_documents
    validate_documents({'policy/custom/rc-model.json':{'arbitrary':'business'}})


def test_build_fingerprint_includes_effective_dockerignore(tmp_path):
    from setup_project import build_fingerprint
    for name in ('one','two'):
        (tmp_path/name).mkdir()
        (tmp_path/name/'Dockerfile').write_text('FROM scratch\nCOPY . /app\n')
    (tmp_path/'one/Dockerfile.dockerignore').write_text('private\n')
    (tmp_path/'two/Dockerfile.dockerignore').write_text('public\n')
    assert build_fingerprint(dict(context=str(tmp_path),dockerfile='one/Dockerfile')) != build_fingerprint(dict(context=str(tmp_path),dockerfile='two/Dockerfile'))


def test_docker_command_cancels_inflight_cli_promptly(tmp_path,monkeypatch):
    from robot_control.runtime_manager import docker_command
    script=tmp_path/'docker';script.write_text('#!/bin/sh\nsleep 20\n');script.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    cancel=threading.Event();timer=threading.Timer(.1,cancel.set);timer.start()
    start=time.monotonic()
    with pytest.raises(RuntimeError,match='cancel'):
        docker_command(['create'],timeout=5,cancel=cancel)
    assert time.monotonic()-start<1.5
    timer.join()


def test_down_preserves_stopped_controller_exit_code(tmp_path,monkeypatch):
    import compose_project
    import robot_control.runtime_manager as module
    from test_runtime_manager import manager
    runtime,engine=manager(tmp_path)
    (tmp_path/'.setup').mkdir();(tmp_path/'.setup/runtime.json').write_text(json.dumps({**runtime.context,'version':3}))
    engine.containers['rc']={'Id':'rc','Config':{'Labels':{'com.docker.compose.project':'base','com.docker.compose.service':'robot-control'}},
        'HostConfig':{},'State':{'Running':False,'ExitCode':1}}
    original=engine.__call__
    def run(args,**kwargs):
        if args[0]=='compose':
            if 'down' in args:engine.containers.clear()
            return ''
        return original(args,**kwargs)
    runtime.runner=run
    monkeypatch.setattr(module,'ComposeRuntimeManager',lambda *a,**k:runtime)
    monkeypatch.setattr(compose_project.subprocess,'run',lambda *a,**k:None)
    monkeypatch.setattr(module,'docker_command',run)
    assert compose_project.down(tmp_path,['down'])==1


@pytest.mark.parametrize('same_anchor',[True,False])
def test_recovery_reclaims_verified_shm_only_in_original_namespace(tmp_path,monkeypatch,same_anchor):
    import posix_ipc
    from rc_policy_sdk.images import _HEADER
    from robot_control.external import ExternalResources
    name='/rc'+uuid.uuid4().hex[:22]
    segment=posix_ipc.SharedMemory(name,flags=posix_ipc.O_CREX,size=_HEADER.size+4)
    import mmap
    with mmap.mmap(segment.fd,_HEADER.size+4) as mapping:
        mapping[:]=_HEADER.pack(4,7)+b'data'
    segment.close_fd()
    record=dict(remote_id='r',owner_id='owner',instance_id='instance',endpoint='http://unused',model_id='m',
                requests={'request':{'head':dict(shm_name=name,pool_id=name[3:15],frame_id=7,nbytes=4)}})
    path=tmp_path/'old.json';path.write_text(json.dumps(dict(version=3,deployment_id='dep',anchor_id='old-anchor',remote_models=[record])))
    monkeypatch.setattr(ExternalResources,'_release_remote',staticmethod(lambda *a,**k:False))
    try:
        if same_anchor:
            ExternalResources.recover(tmp_path,time.monotonic()+2,'dep',anchor_id='old-anchor')
            with pytest.raises(posix_ipc.ExistentialError):posix_ipc.SharedMemory(name)
            assert not path.exists()
        else:
            with pytest.raises(RuntimeError,match='IPC|anchor|namespace'):
                ExternalResources.recover(tmp_path,time.monotonic()+2,'dep',anchor_id='new-anchor')
            assert path.exists()
            remaining=posix_ipc.SharedMemory(name);remaining.close_fd()
    finally:
        try:posix_ipc.unlink_shared_memory(name)
        except posix_ipc.ExistentialError:pass


from test_controller import setup_controller, ready


def test_exit_failure_is_bounded_and_reports_nonzero(setup_controller):
    controller,_,_=setup_controller
    ready(controller)
    original=controller._close_worker
    def unavailable(*args,**kwargs):raise RuntimeError('cleanup proof unavailable')
    controller._close_worker=unavailable
    try:
        controller.submit('exit')
        assert controller.done.wait(1), 'exit must not silently restart an unbounded interactive cleanup loop'
        assert controller.exit_code==1
    finally:
        controller._close_worker=original
        if not controller.done.is_set():
            controller.submit('exit');controller.done.wait(3)


def test_prepare_accepts_prepared_local_image_without_registry_pull(tmp_path,monkeypatch):
    import setup_project
    from types import SimpleNamespace
    (tmp_path/'.setup').mkdir()
    context=dict(version=3,base_project='base',uid=1000,gid=1000)
    (tmp_path/'.setup/runtime.json').write_text(json.dumps(context))
    folder=tmp_path/'policy/custom';folder.mkdir(parents=True)
    (folder/'compose.yaml').write_text('services:\n  policy:\n    image: custom:local\n')
    calls=[]
    def run(command,*args,**kwargs):
        calls.append(command)
        if 'config' in command:
            return SimpleNamespace(stdout=json.dumps({'services':{'policy':{'image':'custom:local'}}}))
        return SimpleNamespace(stdout='[{"Id":"sha256:prepared"}]')
    monkeypatch.setattr(setup_project,'run_command',run)
    setup_project.prepare_images(tmp_path,['custom'],context)
    assert not any('pull' in command for command in calls)


def test_anchor_restart_in_same_container_is_a_new_ipc_generation(tmp_path):
    from robot_control.runtime_manager import ComposeRuntimeManager,BlockedInfrastructure
    from test_runtime_manager import manager
    runtime,engine=manager(tmp_path)
    for identity,service,ipc,started in [('anchor','ipc-anchor','shareable','2026-01-01T00:00:02Z'),
                                        ('rc','robot-control','container:anchor','2026-01-01T00:00:01Z')]:
        engine.containers[identity]={'Id':identity,'Config':{'Labels':{'com.docker.compose.project':'base','com.docker.compose.service':service}},
            'HostConfig':{'IpcMode':ipc},'State':{'Running':True,'StartedAt':started}}
    runtime.anchor_id=None
    with pytest.raises(BlockedInfrastructure,match='IPC|anchor'):
        ComposeRuntimeManager.check_infrastructure(runtime,time.monotonic()+2)
