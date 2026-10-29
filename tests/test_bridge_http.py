"""Real HTTP + managed policy subprocess; optional production Conda/ROS paths."""
from config_helpers import read_config
from project_templates import template_path, copy_templates
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import tempfile

import httpx
import pytest

from robot_control.controller import RobotController
from robot_control.process import PolicyProcess
from test_controller import until

ROOT=Path(__file__).resolve().parents[1]
MODEL_ROOT=Path(os.environ.get('RC_MODEL_SERVER_ROOT','/nonexistent/explicit-RC_MODEL_SERVER_ROOT-required'))
SERVER_PYTHON=os.environ.get('RC_MODEL_SERVER_PYTHON',sys.executable)
CONDA=os.environ.get('RC_BRIDGE_CONDA')
pytestmark=pytest.mark.skipif(not (MODEL_ROOT/'src/model_server').is_dir(),reason='set RC_MODEL_SERVER_ROOT for bridge HTTP integration')


@pytest.fixture
def http_model(tmp_path):
    models=tmp_path/'models'; profile=models/'mock'; profile.mkdir(parents=True)
    (profile/'model.json').write_text(json.dumps({'id':'mock','backend':'mock','conda_env':'model-server-mock',
                                                 'checkpoint':'','model':{},'actions':{'processor':'identity'}}))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
    buffers=tmp_path/'buffers'; buffers.mkdir()
    config={'models_dir':str(models),'buffer_dir':str(buffers),'allowed_shm_roots':['/dev/shm',str(tmp_path.parent),tempfile.gettempdir()],
            'load_timeout':10.,'inference_timeout':5.,'release_timeout':2.,'terminate_timeout':.5,
            'conda_executable':CONDA or 'conda','worker_python':None if CONDA else SERVER_PYTHON}
    script='''
import json,sys,uvicorn
from pathlib import Path
from model_server.api import create_app
from model_server.configuration import Settings
values=json.loads(sys.argv[1])
for key in ('models_dir','buffer_dir'): values[key]=Path(values[key])
values['allowed_shm_roots']=[Path(p) for p in values['allowed_shm_roots']]
uvicorn.run(create_app(Settings(**values)),host='127.0.0.1',port=int(sys.argv[2]),log_level='warning')
'''
    env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(MODEL_ROOT/'src'),str(MODEL_ROOT/'packages/model-client/src')]))
    log=tmp_path/'server.log'
    with log.open('w') as output:
        process=subprocess.Popen([SERVER_PYTHON,'-c',script,json.dumps(config),str(port)],env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        endpoint=f'http://127.0.0.1:{port}'
        try:
            until(lambda: _healthy(endpoint), timeout=10)
            yield endpoint,tmp_path
        finally:
            process.send_signal(signal.SIGTERM)
            try: process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(); pytest.fail(log.read_text())
    assert not list(buffers.iterdir())


def _healthy(endpoint):
    try: return httpx.get(endpoint+'/healthz',timeout=.2).status_code==200
    except httpx.HTTPError: return False


def build_controller(endpoint,path,mode,transport,*,delay=.15,load_delay=0.,ros_robot=None):
    policies=path/'policy'
    copy_templates(ROOT/'policy',policies)
    # Retired HTTP policy: regression coverage for external image leases.
    shutil.copytree(ROOT/'tests/fixtures/legacy_model_server', policies/'model_server')
    manifest=policies/'model_server/policy.yaml'
    config=read_config(manifest)
    config['params'].update(endpoint=endpoint,mode=mode,image_transport=transport,continuous_observations=True)
    config['params']['request_when_remaining']=2
    config['params']['load_overrides']={'model':{'chunk_size':8,'delay_s':delay,'load_delay_s':load_delay}}
    config['params']['timeouts']={'connect_s':1.,'request_s':5.,'load_s':10.,'release_s':3.}
    manifest.write_text(json.dumps(config))
    rc=read_config(template_path(ROOT/'rc.yaml'))
    rc.update(policy_directory=str(policies),startup_policy='model_server',outputs_directory=str(path/'outputs'))
    rc['frequency_hz']=50
    rc['timeouts']={'initialize_s':15.,'inference_s':10.,'release_s':2.}
    rc['conda_executable']=CONDA or 'conda'
    config_path=path/'rc.yaml'; config_path.write_text(json.dumps(rc))
    if CONDA:
        factory=PolicyProcess
    else:
        factory=lambda directory,config,version=1,**kw:PolicyProcess(directory,config,version,python_command=[sys.executable],**kw)
    return RobotController(config_path,process_factory=factory,robot=ros_robot)


def ready(controller):
    controller.start()
    until(lambda:controller.status()['state'] in ('paused','fault'),timeout=15)
    assert controller.status()['state']=='paused',list(controller.events.queue)


def stop(controller):
    controller.submit('exit')
    assert controller.done.wait(15), (controller.status(),list(controller.events.queue))
    assert not controller._pool.directory.exists()
    assert not controller._external.recovery_path.exists()


@pytest.mark.parametrize('mode,transport',[('sync','shm'),('async-add','shm'),('async-add','http')])
def test_bridge_actual_http_per_action_sparse_inference_and_cleanup(http_model,mode,transport):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,mode,transport)
    try:
        ready(controller)
        status=httpx.get(endpoint+'/v1/status').json()
        assert status['owner_id'] and status['state']=='ready'
        policy=controller._worker
        controller.submit('resume')
        until(lambda:controller.status()['consumed']>=12 or controller.status()['state']=='fault',timeout=10)
        assert controller.status()['state']=='running',list(controller.events.queue)
        controller.submit('pause')
        until(lambda:controller.status()['state']=='paused')
        count=controller.status()['consumed']
        time.sleep(.2)
        assert controller.status()['consumed']==count
        controller.submit('resume')
        until(lambda:controller.status()['consumed']>count,timeout=8)
        log=list(controller.events.queue)
        model_calls=sum('model infer ' in row['message'] for row in log)
        callbacks=sum('裁剪' in row['message'] for row in log)
        assert callbacks>=10 and model_calls<callbacks
    finally:
        stop(controller)
    assert controller.exit_code==0,list(controller.events.queue)
    assert not policy.alive
    assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'


def test_exit_during_bridge_initialization_revokes_remote_load(http_model):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,'sync','shm',load_delay=3.)
    controller.start()
    until(lambda:httpx.get(endpoint+'/v1/status').json()['owner_id'] is not None,timeout=12)
    stop(controller)
    assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'


def test_remote_model_release_timeout_reports_nonzero_rc_exit(http_model):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,'sync','shm')
    manifest=path/'policy/model_server/policy.yaml'
    profile=read_config(manifest)
    profile['params']['load_overrides']['model']['release_delay_s']=5.
    profile['params']['timeouts']['release_s']=5.
    controller.config['timeouts']['release_s']=5.
    manifest.write_text(json.dumps(profile))
    # Refresh the not-yet-started registry after editing the test profile.
    controller.registry.get('model_server').config=profile
    ready(controller)
    worker=controller._worker
    stop(controller)
    assert controller.exit_code!=0
    assert any('远端模型已强制终止' in event['message'] for event in controller.events.queue)
    assert not worker.alive
    assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'


def test_killed_bridge_cleans_remote_model_without_reusing_inflight_frames(http_model):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,'async-add','shm',delay=.5)
    ready(controller)
    try:
        controller.submit('resume')
        until(lambda:controller.status()['consumed']>=6,timeout=8)
        until(lambda:httpx.get(endpoint+'/v1/status').json()['busy'],timeout=4)
        with controller._external._lock:
            pinned=[lease.descriptors for r in controller._external._records.values() for lease in r['leases'].values()]
        assert pinned
        before={d['path']:Path(d['path']).read_bytes() for p in pinned for d in p.values()}
        os.killpg(controller._worker.pid,signal.SIGKILL)
        time.sleep(.05)
        for name,data in before.items():
            if Path(name).exists(): assert Path(name).read_bytes()==data
        until(lambda:httpx.get(endpoint+'/v1/status').json()['state']=='unloaded',timeout=8)
        assert controller.status()['state']=='fault'
    finally:
        stop(controller)


def test_bridge_hot_config_cold_reload_and_failure_cleanup(http_model):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,'async-add','shm')
    ready(controller)
    first=controller._worker
    try:
        controller.submit('set policy.params.request_when_remaining 3')
        until(lambda:controller.status()['config_version']==2)
        assert controller._worker is first
        controller.submit('save-config policy')
        manifest=path/'policy/model_server/policy.yaml'
        until(lambda:read_config(manifest)['params']['request_when_remaining']==3)
        controller.submit('set policy.params.mode "sync"')
        until(lambda:controller._worker is not first and controller.status()['state']=='paused',timeout=10)
        assert not first.alive
        controller.submit('set policy.params.load_overrides.model.fail_load true')
        # Existing optional model override keys must be edited as a whole cold object.
        time.sleep(.1)
        controller.submit('set policy.params.load_overrides {"model":{"fail_load":true}}')
        until(lambda:controller.status()['state']=='fault',timeout=10)
        assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'
    finally:
        stop(controller)


@pytest.mark.skipif(not CONDA,reason='CLI signal acceptance uses real isolated Conda environments')
@pytest.mark.parametrize('signum',[signal.SIGINT,signal.SIGTERM])
def test_bridge_cli_signals_release_remote_and_exit_zero(http_model,signum):
    endpoint,path=http_model
    controller=build_controller(endpoint,path,'async-add','shm',delay=.4)
    controller._pool.close()  # Only use its saved configuration for the CLI process.
    log=path/'rc-cli.log'
    with log.open('w') as output:
        process=subprocess.Popen([sys.executable,'-m','robot_control','--config',str(controller.config_path),'--headless'],
                                 stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        try:
            until(lambda:httpx.get(endpoint+'/v1/status').json()['state']=='ready',timeout=15)
            # Ready model precedes policy ready by one socket round trip.
            until(lambda:'等待 resume' in log.read_text(),timeout=5)
            process.stdin.write('resume\n'); process.stdin.flush()
            until(lambda:httpx.get(endpoint+'/v1/status').json()['busy'],timeout=5)
            process.send_signal(signum)
            process.wait(15)
            assert process.returncode==0,log.read_text()
            assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'
            assert not list((path/'outputs/recovery').glob('*.json'))
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill();process.wait()
    time.sleep(.2)
    assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'
