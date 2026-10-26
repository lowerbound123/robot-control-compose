"""Python 3.10 RC -> amd64 Python 3.11 host -> local HTTP model, real shm/UDS."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

import numpy as np
from PIL import Image
import pytest

from rc_policy_sdk.images import ImagePool
from rc_policy_sdk.wire import encode_message
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from robot_control.controller import RobotController
from robot_control.host_client import HostClient
from robot_control.process import PolicyProcess
from robot_control.robots import MockRobot
from llm_http_helpers import response_for

pytestmark = pytest.mark.skipif(os.getenv('RC_LLM_DOCKER_TEST') != '1', reason='requires isolated LLM Compose test project')
ROOT = Path('/workspace/policy')
HOSTS = {'llm-direction': {'socket': '/run/robot-control/llm-direction.sock'}}


def http(path, payload=None):
    request = urllib.request.Request('http://llm-model:8080' + path,
        data=None if payload is None else json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as result:
        return json.load(result)


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('cross-container LLM operation timed out')


def config():
    return json.loads((ROOT / 'llm_direction/policy.json').read_text())


@pytest.fixture
def frames():
    pool = ImagePool()
    for name, value in zip(('head', 'left_wrist', 'right_wrist'), (25, 75, 125)):
        pool.publish(name, np.full((240, 320, 3), value, np.uint8), time.monotonic())
    lease = pool.acquire()
    request = {'request_id': 1, 'generation': 1, 'config_version': 1, 'action_chunk': [],
               'state': {'qpos': [0] * 14, 'eef': [0] * 14}, 'images': lease.descriptors}
    try:
        yield pool, lease, request
    finally:
        lease.release()
        pool.close()


@pytest.mark.parametrize('provider,format', [('deepseek', 'json_object'), ('openai_compatible', 'text'),
                                          ('openai', 'json_schema'), ('openai_decisions', 'choice')])
def test_three_camera_contract_across_versions_and_all_endpoints(frames, provider, format):
    assert sys.version_info[:2] == (3, 10)
    client = HostClient.get(HOSTS['llm-direction']['socket'], 'llm-direction')
    assert client.preflight('llm_direction')['python'].startswith('3.11.')
    http('/control', {'responses': [{'body': response_for(provider)}]})
    cfg = config()
    cfg['params']['provider'].update(type=provider, output_format=format)
    worker = PolicyProcess(ROOT / 'llm_direction', cfg, policy_hosts=HOSTS, init_timeout_s=30,
                           inference_timeout_s=10, release_timeout_s=3)
    _, _, request = frames
    assert len(encode_message(request)) < 4096
    assert b'base64' not in encode_message(request)
    try:
        worker.start()
        assert http('/requests') == []
        worker.submit(request)
        result = wait(worker.poll)
        assert result['type'] == 'result' and result['images_released'] is True, result
        assert result['actions'][-1][2] == pytest.approx(.05)
        requests = http('/requests')
        assert len(requests) == 1
        body = requests[0]['body']
        parts = body['input'][0]['content'] if provider == 'openai_decisions' else (
            body['input'][1]['content'] if provider == 'openai' else body['messages'][1]['content'])
        encoded = [part['image_url'] for part in parts if part['type'] in ('input_image', 'image_url')]
        for value, expected in zip(encoded, (25, 75, 125)):
            url = value['url'] if isinstance(value, dict) else value
            with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as image:
                pixels = np.asarray(image).astype(int)
                assert pixels.shape == (240, 320, 3)
                assert np.max(abs(pixels - expected)) <= 1
        assert len(encoded) == 3
    finally:
        worker.close()
    assert worker.cleanup_confirmed and not worker.has_readers


def test_native_aba_across_container(frames):
    http('/control', {})
    chain = ChainProcess(PolicyRegistry(ROOT).get('default_chain'), policy_hosts=HOSTS,
                         init_timeout_s=30, inference_timeout_s=10, release_timeout_s=3)
    try:
        chain.start()
        assert len(chain._workers) == 2
        chain.submit(frames[2])
        result = wait(chain.poll)
        assert result['type'] == 'result' and result['images_released'] is True, result
        assert len(http('/requests')) == 1
        assert result['actions'][-1][2] == pytest.approx(.05)
    finally:
        chain.close()
    assert chain.cleanup_confirmed


def test_timeout_does_not_free_frames_without_proof(frames):
    http('/control', {'responses': [{'delay': 2}]})
    worker = PolicyProcess(ROOT / 'llm_direction', config(), policy_hosts=HOSTS,
                           init_timeout_s=30, inference_timeout_s=.4, release_timeout_s=1)
    pool, lease, request = frames
    held = []
    try:
        worker.start()
        worker.submit(request)
        wait(lambda: http('/requests'))
        for value in (2, 3):
            pool.publish('head', np.full((240, 320, 3), value, np.uint8), time.monotonic())
            held.append(pool.acquire())
        assert not pool.publish('head', np.zeros((240, 320, 3), np.uint8), time.monotonic())
        result = wait(worker.poll)
        assert result['type'] == 'error'
        assert result.get('images_released') is not True
        assert not pool.publish('head', np.zeros((240, 320, 3), np.uint8), time.monotonic())
        worker.close()
        assert worker.cleanup_confirmed
        lease.release()
        assert pool.publish('head', np.zeros((240, 320, 3), np.uint8), time.monotonic())
    finally:
        worker.close()
        for item in held:
            item.release()


@pytest.mark.parametrize('action', ['pause', 'take', 'set policy.params.task "updated"'])
def test_controller_cancels_late_result_across_container(tmp_path, action):
    http('/control', {'responses': [{'delay': 1}]})
    cfg = json.loads((ROOT / 'rc.json').read_text())
    cfg.update(outputs_directory=str(tmp_path / 'outputs'), frequency_hz=50)
    cfg['safety']['enabled'] = False
    cfg['timeouts'] = {'initialize_s': 30, 'inference_s': 8, 'release_s': 3}
    path = tmp_path / 'rc.json'
    path.write_text(json.dumps(cfg))
    controller = RobotController(path, robot=MockRobot())
    try:
        controller.start()
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        old = controller._worker
        controller.submit('resume')
        wait(lambda: http('/requests'))
        operation = controller.submit(action)
        wait(lambda: controller.get_operation(operation)['status'] in ('succeeded', 'failed'))
        assert controller.get_operation(operation)['status'] == 'succeeded'
        wait(lambda: not controller.status()['in_flight'])
        assert controller.status()['consumed'] == 0
        assert controller.status()['queue_remaining'] == 0
        assert controller.status()['control_owner'] == ('mcp' if action == 'take' else 'policy')
        if action.startswith('set'):
            assert old.cleanup_confirmed and controller._worker.runtime_id != old.runtime_id
    finally:
        controller.submit('exit')
        assert controller.done.wait(20)
