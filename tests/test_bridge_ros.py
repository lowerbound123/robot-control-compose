"""Actual rclpy simulator + three isolated Conda processes + HTTP bridge."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import httpx
import pytest

from test_bridge_http import http_model as http_model
from test_ros_controller_integration import ros_rc as ros_rc, until

pytestmark=pytest.mark.skipif(not os.environ.get('RC_BRIDGE_CONDA'),reason='set RC_BRIDGE_CONDA and RC_TEST_CONDA; source ROS overlay')


@pytest.mark.parametrize('mode',['sync','async-add'])
def test_actual_ros_http_bridge_pose_modes_switch_exit(ros_rc,http_model,mode):
    assert importlib.util.find_spec('rclpy') is not None
    endpoint,path=http_model
    controller=ros_rc.controller
    directory=controller.registry.directory/'model_server'
    manifest=directory/'policy.json'
    config=json.loads(manifest.read_text())
    config['params'].update(endpoint=endpoint,mode=mode,continuous_observations=True)
    config['params']['request_when_remaining']=3
    config['params']['load_overrides']={'model':{'chunk_size':12,'delay_s':.25}}
    manifest.write_text(json.dumps(config))
    proof=path/'bridge-prefix.json'
    config['params']['proof']=str(proof)
    manifest.write_text(json.dumps(config))
    main=directory/'main.py'
    main.write_text(main.read_text()+'''
import sys,json,importlib.util
from pathlib import Path
_base_initialize=initialize
def initialize(config,*,runtime):
    for package in ('rclpy','robot_control','textual','torch','transformers'):
        assert importlib.util.find_spec(package) is None, package
    context=_base_initialize(config,runtime=runtime)
    Path(config['params']['proof']).write_text(json.dumps({'prefix':sys.prefix,'sdk':__import__('rc_policy_sdk').__file__}))
    return context
''')
    old=controller._worker
    controller.submit('switch model_server')
    until(lambda:controller.status()['policy']=='model_server' and controller.status()['state']=='paused',controller)
    worker=ros_rc.remember_worker()
    assert not old.alive
    assert Path(json.loads(proof.read_text())['prefix']).name=='rc-policy-model-server'
    assert Path(sys.prefix).name=='robot-control'
    assert Path(httpx.get(endpoint+'/v1/status').json()['worker_runtime']['prefix']).name=='model-server-mock'
    assert controller._pool.directory.parent==Path('/dev/shm')
    controller.submit('resume')
    until(lambda:controller.status()['consumed']>=18,controller)
    controller.submit('save bridge_pose')
    pose=controller.outputs/'state_saves/bridge_pose.json'
    until(lambda:pose.exists() and controller.status()['state']=='paused',controller)
    count=controller.status()['consumed']
    controller.submit('reset')
    until(lambda:controller.status()['hold_name']=='reset' and controller.status()['state']=='paused',controller)
    controller.submit('load bridge_pose')
    until(lambda:controller.status()['hold_name']=='bridge_pose' and controller.status()['state']=='paused',controller)
    assert controller.status()['consumed']==count
    controller.submit('resume')
    until(lambda:controller.status()['consumed']>count+3,controller)
    # Ready model unload is tied to bridge release when switching away.
    controller.submit('switch hold')
    until(lambda:controller.status()['policy']=='hold' and controller.status()['state']=='paused',controller)
    ros_rc.remember_worker()
    assert not worker.alive
    assert httpx.get(endpoint+'/v1/status').json()['state']=='unloaded'
    assert not controller._external.recovery_path.exists()
    time.sleep(.05)
