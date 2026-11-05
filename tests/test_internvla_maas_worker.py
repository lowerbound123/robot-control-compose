"""Production entry, real SDK host/worker and RC; only the remote model is fake."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

from config_helpers import read_config
from host_helpers import LocalPolicyProcess
from internvla_maas_helpers import ROOT, images, model_server, settings, wait
from project_templates import copy_templates
from rc_policy_sdk.images import ImagePool

pytest.importorskip('websockets')
pytest.importorskip('msgpack')
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason='local MaaS worker requires Python 3.11')


def prepare(tmp_path, server, monkeypatch):
    root = tmp_path / 'policy'
    directory = root / 'internvla_maas'
    copy_templates(ROOT / 'policy/internvla_maas', directory)
    (directory / 'model.json').write_text(json.dumps(settings(server.address)))
    sources = [ROOT / 'src', ROOT / 'packages/policy-sdk/src', ROOT / 'packages/internvla-maas-policy/src']
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(map(str, sources)))
    return directory, read_config(directory / 'policy.yaml')


def test_real_worker_releases_shared_images_and_reopens_context(tmp_path, monkeypatch):
    with model_server() as server:
        directory, manifest = prepare(tmp_path, server, monkeypatch)
        worker = LocalPolicyProcess(directory, manifest, init_timeout_s=5, inference_timeout_s=3, release_timeout_s=3)
        pool = ImagePool(tmp_path / 'frames', cameras=('head', 'left_wrist', 'right_wrist'))
        for name, frame in images().items():
            pool.publish(name, frame, 1.)
        lease = pool.acquire()
        try:
            worker.start()
            qpos = [0.25] * 14
            qpos[6] = qpos[13] = -1
            request = {'request_id': 1, 'generation': 1, 'config_version': 1,
                       'state': {'qpos': qpos}, 'action_chunk': [], 'images': lease.descriptors,
                       'runtime': {'observation_id': 'maas-one', 'control_epoch': 1, 'consumed_actions': 0}}
            worker.submit(request)
            response = wait(worker.poll)
            assert response['type'] == 'result' and response['images_released'] is True, response
            actions = np.asarray(response['actions'])
            assert actions.shape == (50, 14)
            np.testing.assert_allclose(actions[:, [6, 13]], -.85)
            assert server.requests[0]['images']['front'][0, 0, 0] == 1
            worker.reset_context()
            worker.submit({**request, 'request_id': 2, 'generation': 2,
                           'runtime': {'observation_id': 'maas-two', 'control_epoch': 2, 'consumed_actions': 7}})
            response = wait(worker.poll)
            assert response['type'] == 'result' and response['images_released'] is True, response
            assert server.requests[0]['episode_id'] != server.requests[1]['episode_id']
        finally:
            forced = worker.close()
            assert worker.cleanup_confirmed and not worker.has_readers
            lease.release()
            pool.close()
        assert not forced


def test_monitor_and_hot_scheduling_work_during_real_worker_inference(tmp_path, monkeypatch):
    with model_server() as server:
        directory, manifest = prepare(tmp_path, server, monkeypatch)
        worker = LocalPolicyProcess(directory, manifest, init_timeout_s=5, inference_timeout_s=3, release_timeout_s=3)
        pool = ImagePool(tmp_path / 'frames', cameras=('head', 'left_wrist', 'right_wrist'))
        for name, frame in images().items():
            pool.publish(name, frame, 1.)
        lease = pool.acquire()
        gate = threading.Event()
        def monitor():
            assert worker.submit_monitor(1)
            response = wait(worker.poll_monitor)
            assert response['type'] == 'monitor_result', response
            return response['result']
        try:
            worker.start()
            identity = worker.runtime_id
            assert worker.supports_monitor
            empty = {'avg_latency_ms': '—', 'median_latency_ms': '—', 'last_latency_ms': '—', 'failed_requests': 0}
            initial = monitor()
            assert {key: initial[key] for key in empty} == empty
            server.responses.put({'gate': gate})
            worker.submit({'request_id': 1, 'generation': 1, 'config_version': 1,
                           'state': {'qpos': [0.] * 14}, 'action_chunk': [], 'images': lease.descriptors,
                           'runtime': {'control_epoch': 1, 'consumed_actions': 0}})
            wait(lambda: len(server.requests) == 1)
            pending = monitor()
            assert {key: pending[key] for key in empty} == empty and worker.poll() is None
            gate.set()
            response = wait(worker.poll)
            assert response['type'] == 'result' and response['images_released'] is True
            completed = monitor()
            assert completed['avg_latency_ms'] > 0
            assert completed['avg_latency_ms'] == completed['median_latency_ms'] == completed['last_latency_ms']
            assert completed['failed_requests'] == 0
            for version, mode in ((2, 'sync'), (3, 'async')):
                worker.update_config({'execution.mode': mode, 'execution.call_every_actions': version}, version)
                assert worker.runtime_id == identity and worker.config_version == version
                assert len(server.hellos) == 1 and len(server.requests) == 1
                assert monitor() == completed
            worker.reset_context()
            assert monitor() == completed and len(server.hellos) == 2
        finally:
            gate.set()
            forced = worker.close()
            assert worker.cleanup_confirmed and not worker.has_readers
            lease.release()
            pool.close()
        assert not forced


def test_controller_cold_switch_fault_stop_and_manual_recovery(tmp_path, monkeypatch):
    from robot_control.controller import RobotController
    from robot_control.robots import MockRobot

    class CameraRobot(MockRobot):
        def read_images(self):
            return {name: (frame, time.monotonic()) for name, frame in images().items()}

    with model_server() as server:
        directory, _ = prepare(tmp_path, server, monkeypatch)
        raw = settings(server.address)
        raw['runtime']['instruction'] = 'New task.'
        raw['actions'] = {'gripper_compensation': False}
        # LocalPolicyProcess copies the source directory when it starts; place
        # this candidate before that copy, as a real bind mount would expose it.
        (directory / 'alternate.json').write_text(json.dumps(raw))
        rc = read_config(ROOT / 'configs/rc.mock.yaml.example')
        rc.update(policy_directory=str(directory.parent), startup_policy='internvla_maas',
                  outputs_directory=str(tmp_path / 'outputs'), frequency_hz=30,
                  timeouts=dict(initialize_s=5, inference_s=3, release_s=3, shutdown_s=15))
        rc['safety']['enabled'] = False
        path = tmp_path / 'rc.yaml'
        path.write_text(json.dumps(rc))
        controller = RobotController(path, robot=CameraRobot(), process_factory=LocalPolicyProcess)
        def command(line):
            identity = controller.submit(line)
            result = controller.wait_operation(identity, 12)
            assert result['status'] == 'succeeded', json.dumps(result, ensure_ascii=False)
        controller.start()
        try:
            wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
            old = controller._worker
            command('resume')
            wait(lambda: controller.status()['consumed'] >= 23)
            sessions = len(server.hellos)  # resume performs its ordinary context reset.
            for line in ('set policy.execution.mode "sync"',
                         'set policy.execution.call_every_actions 3',
                         'set policy.execution.mode "async"'):
                version = controller.status()['config_version']
                command(line)
                assert controller.status()['config_version'] == version + 1
                assert controller.status()['state'] == 'running'
                assert controller._worker is old and len(server.hellos) == sessions
            command('pause')
            command('policy internvla_maas switch-model alternate.json')
            assert controller.status()['state'] == 'paused'
            assert old.cleanup_confirmed and not old.has_readers
            assert controller._worker.runtime_id != old.runtime_id
            server.responses.put({'body': {'type': 'stop', 'reason': 'fixture-key operator stop'}})
            command('resume')
            wait(lambda: controller.status()['state'] == 'fault' and controller._worker is None)
            count = controller.status()['consumed']
            assert controller.status()['queue_remaining'] == 0
            requests = len(server.requests)
            time.sleep(.15)
            assert len(server.requests) == requests and controller.status()['consumed'] == count
            assert server.requests[-1]['instruction'] == 'New task.'
            assert 'fixture-key' not in json.dumps(controller.read_events())
            assert 'fixture-key' not in json.dumps(controller.status())
            command('switch internvla_maas')
            assert controller.status()['state'] == 'paused'
            command('resume')
            wait(lambda: controller.status()['consumed'] > count)
        finally:
            controller.submit('exit')
            assert controller.done.wait(20), controller.read_events()
        assert controller.exit_code == 0
