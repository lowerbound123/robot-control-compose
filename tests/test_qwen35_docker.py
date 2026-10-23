"""Python 3.10 RC against two real Python 3.11 Qwen-image containers."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from rc_policy_sdk.wire import encode_message
from robot_control.process import PolicyProcess
from qwen_helpers import IDENTITIES, events, prepare_policies, set_controls, wait

pytestmark = pytest.mark.skipif(os.environ.get('RC_QWEN_DOCKER_TEST') != '1',
                               reason='requires Qwen Compose integration containers')
ROOT = Path('/workspace/policy')
HOSTS = {identity.replace('_', '-'): {'socket': f"/run/robot-control/{identity.replace('_', '-')}.sock"}
         for identity in IDENTITIES}


def probe(worker):
    worker.submit_command(uuid.uuid4().hex, 'probe', [])
    def poll():
        response = worker.poll_command()
        return response if response and response['type'] != 'command_started' else None
    response = wait(poll)
    assert response['type'] == 'command_result', response
    return response['result']


@pytest.fixture
def case():
    prepare_policies(ROOT)
    workers, leases = [], []
    pool = ImagePool(slots=1)
    for index, name in enumerate(('head', 'left_wrist', 'right_wrist'), 1):
        pool.publish(name, np.full((240, 320, 3), index, np.uint8), time.time())
    def worker(identity=IDENTITIES[0], **limits):
        config = json.loads((ROOT / identity / 'policy.json').read_text())
        obj = PolicyProcess(ROOT / identity, config, policy_hosts=HOSTS,
                            **({'init_timeout_s': 15, 'inference_timeout_s': 5, 'release_timeout_s': 1} | limits))
        workers.append(obj)
        return obj
    serial = 0
    def submit(obj, chunk=(), count=0):
        nonlocal serial
        serial += 1
        lease = pool.acquire()
        leases.append(lease)
        request = dict(request_id=serial, generation=1, config_version=1,
                       state={'qpos': [0] * 14}, action_chunk=list(chunk), images=lease.descriptors,
                       runtime={'consumed_actions': count, 'control_epoch': 1, 'observation_id': str(serial)})
        # Pixel arrays are absent from JSON: only descriptors and small action/state data.
        assert len(encode_message(request)) < 16000
        obj.submit(request)
        return lease
    try:
        yield worker, submit, pool
    finally:
        for obj in workers:
            obj.close()
            assert obj.cleanup_confirmed and not obj.has_readers
        for lease in leases:
            lease.release()
        pool.close()
        assert pool._bytes == 0


def result(worker):
    response = wait(worker.poll)
    assert response['type'] == 'result' and response['images_released'] is True, response
    return response['actions']


def test_same_environment_separate_runtime_and_three_camera_pixels(case):
    make, submit, _ = case
    assert sys.version_info[:2] == (3, 10) and np.__version__ == '1.26.4'
    workers = [make(identity) for identity in IDENTITIES]
    for obj in workers:
        assert obj.preflight()['python'].startswith('3.11.')
        obj.start()
    assert workers[0].host.instance_id != workers[1].host.instance_id
    assert workers[0].runtime_id != workers[1].runtime_id
    leases = [submit(obj) for obj in workers]
    for obj, value, workflow, lease in zip(workers, (11, 22), ('single', 'two_stage'), leases):
        actions = result(obj)
        assert np.asarray(actions).shape == (50, 14)
        assert actions[0] == [value] * 6 + [-3] + [value] * 6 + [-3]
        lease.release()
        state = probe(obj)
        assert state['python'].startswith('3.11.') and state['numpy'] == '2.2.6'
        assert state['workflow'] == workflow and state['calls'] == 1
        assert events(ROOT / obj.config['id'], 'predicted')[-1]['pixels'] == {
            'head': 230400, 'left_wrist': 460800, 'right_wrist': 691200}
    workers[0].close()
    assert workers[1].runnable and probe(workers[1])['calls'] == 1


def test_background_inference_owns_copies_after_images_released(case):
    make, submit, pool = case
    obj = make()
    obj.start()
    lease = submit(obj)
    actions = result(obj)
    lease.release()
    directory = ROOT / IDENTITIES[0]
    set_controls(directory, block_infer=True)
    lease = submit(obj, actions[21:], count=21)
    remaining = result(obj)  # ACK while the second model inference is blocked.
    assert len(remaining) == 29
    wait(lambda: len(events(directory, 'infer')) == 2)
    lease.release()
    for name in ('head', 'left_wrist', 'right_wrist'):
        assert pool.publish(name, np.full((240, 320, 3), 99, np.uint8), time.time())
    set_controls(directory, block_infer=False)
    wait(lambda: len(events(directory, 'predicted')) == 2)
    assert events(directory, 'predicted')[-1]['pixels']['head'] == 230400
    lease = submit(obj, remaining, count=21)
    assert len(result(obj)) == 50
    lease.release()


@pytest.mark.parametrize('failure', ['timeout', 'crash', 'release'])
def test_one_host_failure_does_not_terminate_other_host(case, failure):
    make, submit, pool = case
    bad, good = make(inference_timeout_s=.4, release_timeout_s=.2), make(IDENTITIES[1])
    bad.start()
    good.start()
    directory = ROOT / IDENTITIES[0]
    if failure == 'release':
        set_controls(directory, block_close=True)
        assert bad.close() is True
    else:
        set_controls(directory, **({'block_infer': True} if failure == 'timeout' else {'crash_infer': True}))
        lease = submit(bad)
        wait(lambda: events(directory, 'infer'))
        assert not pool.publish('head', np.zeros((240, 320, 3), np.uint8), time.time())
        response = wait(bad.poll)
        assert response['type'] != 'result'
        bad.close()
        assert bad.cleanup_confirmed
        lease.release()  # Only the confirmed old-process exit releases this frame.
        assert pool.publish('head', np.ones((240, 320, 3), np.uint8), time.time())
    assert not bad.has_readers
    assert good.runnable and probe(good)['workflow'] == 'two_stage'


def test_cancel_initialization_cleans_old_runtime_before_next_load(case):
    make, _, _ = case
    directory = ROOT / IDENTITIES[0]
    set_controls(directory, block_load=True)
    old = make(release_timeout_s=.2)
    cancel = threading.Event()
    errors = []
    def start():
        try:
            old.start(cancel_event=cancel)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=start)
    thread.start()
    try:
        wait(lambda: events(directory, 'load'))
        cancel.set()
        thread.join(10)
        assert not thread.is_alive() and errors
        assert old.cleanup_confirmed and not old.has_readers
        set_controls(directory, block_load=False)
        fresh = make()
        fresh.start()
        assert fresh.runtime_id != old.runtime_id
        assert len(events(directory, 'warmup')) == 1
    finally:
        cancel.set()
        old.close()
        thread.join(10)


def test_controller_switch_model_uses_real_remote_reload_receipt(case, tmp_path):
    from robot_control.controller import RobotController
    from test_qwen35_worker import ThreeCameraRobot, completed
    config = json.loads(Path('/opt/app/configs/rc.mock.json').read_text())
    config.update(policy_directory=str(ROOT), startup_policy=IDENTITIES[0], policy_hosts=HOSTS,
                  outputs_directory=str(tmp_path / 'outputs'), frequency_hz=100,
                  timeouts={'initialize_s': 15, 'inference_s': 5, 'release_s': 1})
    config['safety']['enabled'] = False
    path = tmp_path / 'rc.json'
    path.write_text(json.dumps(config))
    controller = RobotController(path, robot=ThreeCameraRobot())
    controller.start()
    try:
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        old = controller._worker
        assert completed(controller, 'resume')['status'] == 'succeeded'
        wait(lambda: controller.status()['consumed'] >= 23)
        directory = ROOT / IDENTITIES[0]
        assert completed(controller, f'switch-model {directory}/model.json')['status'] == 'succeeded'
        assert controller.status()['state'] == 'paused' and old.cleanup_confirmed
        assert controller._worker.runtime_id != old.runtime_id
        rows = events(directory)
        loads = [i for i, row in enumerate(rows) if row['phase'] == 'load']
        assert len(loads) == 2
        assert loads[0] < next(i for i, row in enumerate(rows) if row['phase'] == 'close') < loads[1]
    finally:
        controller.submit('exit')
        assert controller.done.wait(20)
