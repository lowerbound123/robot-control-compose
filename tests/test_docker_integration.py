"""Run in the Humble test image, talking to separate Python 3.14 containers."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from rc_policy_sdk.wire import encode_message
from robot_control.host_client import HostClient
from robot_control.process import PolicyProcess
from robot_control.controller import RobotController
from robot_control.config import PolicyRegistry
from robot_control.chain_process import ChainProcess

pytestmark=pytest.mark.skipif(os.environ.get('RC_DOCKER_TEST') != '1', reason='requires Compose integration profile')
ROOT=Path('/workspace/policy')
HOSTS={'hold':{'socket':'/run/robot-control/hold.sock'}, 'second':{'socket':'/run/robot-control/second.sock'}}


def wait(predicate, timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value: return value
        time.sleep(.01)
    raise AssertionError('integration condition timed out')


def test_versions_and_three_camera_payload_cross_interpreters():
    assert sys.version_info[:2] == (3,10)
    assert np.__version__ == '1.26.4'
    client=HostClient.get(HOSTS['hold']['socket'],'hold')
    assert client.preflight('A')['python'].startswith('3.14.')
    cfg=json.loads((ROOT/'A/policy.json').read_text())
    worker=PolicyProcess(ROOT/'A',cfg,policy_hosts=HOSTS,init_timeout_s=5,inference_timeout_s=3,release_timeout_s=1)
    pool=ImagePool(); leases=[]
    try:
        for name,value in [('head',1),('left_wrist',2),('right_wrist',3)]:
            pool.publish(name,np.full((240,320,3),value,np.uint8),1.)
        lease=pool.acquire();leases.append(lease)
        request=dict(request_id=1,generation=1,config_version=1,state={'qpos':[0]*14,
            'image_values':{'head':1,'left_wrist':2,'right_wrist':3}},action_chunk=[],images=lease.descriptors)
        assert len(encode_message(request)) < 2048
        worker.start();worker.submit(request)
        response=wait(worker.poll)
        assert response['images_released'] is True
        assert response['actions'][0][:5] == [11,1,230400,460800,691200]
    finally:
        worker.close()
        for lease in leases:lease.release()
        pool.close()
    assert pool._bytes==0


def test_cross_container_slow_readers_pin_slots_and_resize_safely():
    import posix_ipc
    config = json.loads((ROOT/'A/policy.json').read_text())
    workers, leases = [], []
    pool = ImagePool()
    try:
        for host in ('hold', 'second'):
            cfg = {**config, 'policy_host':host, 'params':{**config['params'], 'delay':.15}}
            worker = PolicyProcess(ROOT/'A', cfg, policy_hosts=HOSTS,
                init_timeout_s=5, inference_timeout_s=3, release_timeout_s=1)
            workers.append(worker); worker.start()
        for name in ('head', 'left_wrist', 'right_wrist'):
            pool.publish(name, np.ones((2,2), np.uint8), 1.)
        first, second = pool.acquire(), pool.acquire()
        leases.extend([first, second])
        old = first.descriptors['head']['shm_name']
        for worker, lease in zip(workers, (first, second)):
            worker.submit(dict(request_id=1,generation=1,config_version=1,
                state={'qpos':[0]*14, 'image_values':dict.fromkeys(lease.descriptors, 1)},
                action_chunk=[],images=lease.descriptors))
        for value in (2, 3):
            assert pool.publish('head', np.full((2,2),value,np.uint8), float(value))
            leases.append(pool.acquire())
        assert not pool.publish('head', np.zeros((2,2),np.uint8), 4.)
        for worker in workers:
            response = wait(worker.poll)
            assert response['type']=='result' and response['images_released'] is True
            assert response['actions'][0][2:5]==[4,4,4]
        first.release()
        assert not pool.publish('head', np.zeros((3,3),np.uint8), 5.)
        second.release()
        assert pool.publish('head', np.zeros((3,3),np.uint8), 6.)
        with pytest.raises(posix_ipc.ExistentialError):
            posix_ipc.SharedMemory(old)
    finally:
        for worker in workers: worker.close()
        for lease in leases: lease.release()
        pool.close()
    assert pool._bytes==0


@pytest.mark.parametrize('split', [False,True])
def test_chain_reuses_instances_in_shared_and_separate_containers(split):
    registry=PolicyRegistry(ROOT)
    b=registry.get('B')
    if split:
        config={**b.config,'policy_host':'second'}
        registry.commit('B',config)
    record=registry.propose_chain(['A','B','A'])
    chain=ChainProcess(record,policy_hosts=HOSTS,init_timeout_s=5,inference_timeout_s=3,release_timeout_s=1)
    pool=ImagePool()
    for name in ('head','left_wrist','right_wrist'):pool.publish(name,np.ones((2,2),np.uint8),1.)
    lease=pool.acquire()
    try:
        chain.start()
        a,b=chain._workers['A'],chain._workers['B']
        if split: assert a.runtime_id != b.runtime_id and a.host.host_id != b.host.host_id
        else: assert a.runtime_id==b.runtime_id and a.pid==b.pid
        chain.submit(dict(request_id=1,generation=1,config_version=record.version,state={'qpos':[0]*14},action_chunk=[],images=lease.descriptors))
        result=wait(chain.poll)
        assert result['type']=='result',result
        assert result['actions'][0][:5]==[11,2,4,4,4]
    finally:chain.close();lease.release();pool.close()
    assert chain.cleanup_confirmed and not chain.has_readers


def test_humble_ros_controller_policy_container_closed_loop(tmp_path,monkeypatch):
    import rclpy
    from arx5_arm_msg.msg import RobotCmd,RobotStatus
    assert len(RobotCmd().joint_pos)==6 and len(RobotStatus().joint_pos)==7
    monkeypatch.setenv('ROS_LOCALHOST_ONLY','1')
    namespace='/compose_test_'+uuid.uuid4().hex[:8]
    cfg=json.loads(Path('/opt/app/configs/rc.ros-test.json').read_text())
    cfg.update(policy_directory=str(ROOT),policy_hosts=HOSTS,outputs_directory=str(tmp_path/'outputs'))
    cfg['robot']=json.loads(json.dumps(cfg['robot']).replace('/robot_control_test',namespace))
    cfg['reset_qpos']=[.02]*14
    cfg['timeouts']={'initialize_s':10,'inference_s':3,'release_s':2}
    path=tmp_path/'rc.json';path.write_text(json.dumps(cfg))
    with (tmp_path/'sim.log').open('w') as log:
        sim=subprocess.Popen([sys.executable,'-m','robot_control_test_sim.simulator','--namespace',namespace],
            stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        controller=RobotController(path)
        try:
            controller.start()
            wait(lambda:controller.status()['state'] in ('paused','fault'))
            assert controller.status()['state']=='paused',list(controller.events.queue)
            controller.submit('resume')
            wait(lambda:controller.status()['consumed']>=4)
            operation=controller.submit('reset')
            wait(lambda:controller.get_operation(operation)['status'] in ('succeeded','failed'))
            assert controller.get_operation(operation)['status']=='succeeded'
            wait(lambda:controller.status()['state']=='paused')
            wait(lambda:np.max(np.abs(np.asarray(controller.robot.read_state()['qpos'])-.02))<.005)
            controller.submit('exit');assert controller.done.wait(12)
            assert controller.exit_code==0 and controller._pool._bytes==0
        finally:
            if not controller.done.is_set():controller.submit('exit');controller.done.wait(12)
            os.killpg(sim.pid,signal.SIGTERM)
            try:sim.wait(5)
            except subprocess.TimeoutExpired:os.killpg(sim.pid,signal.SIGKILL);sim.wait()
