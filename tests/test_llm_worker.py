"""Production policy entry, real workers/chain/controller, local HTTP model only."""
from config_helpers import read_config
from project_templates import template_path, copy_templates
import base64
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import numpy as np
from PIL import Image
import pytest

pytest.importorskip('openai', reason='real LLM workers require the optional LLM environment')
from rc_policy_sdk.images import ImagePool
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from robot_control.controller import RobotController
from host_helpers import LocalPolicyProcess as PolicyProcess
from robot_control.robots import MockRobot
from llm_http_helpers import model_server, response_for

ROOT = Path(__file__).resolve().parents[1]


def wait(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('LLM integration condition timed out')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_TEST_KEY', 'local-test-only')
    monkeypatch.setenv('UNRELATED_SECRET', 'never-inherit-this')
    root = tmp_path / 'policy'
    root.mkdir()
    with model_server() as server:
        config = read_config(template_path(ROOT / 'policy/llm_direction/policy.yaml'))
        config['params']['provider'].pop('api_key_env', None)
        config['params'].update(task='move up then stop', chunk_size=3)
        config['params']['provider'].update(base_url=server.url, api_key='local-test-only', timeout_s=4.)
        for name in ('llm_direction', 'b'):
            folder = root / name
            copy_templates(ROOT / 'policy/llm_direction', folder)
            cfg = {**copy.deepcopy(config), 'id': name}
            (folder / 'policy.yaml').write_text(json.dumps(cfg))
        (root / 'b/main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return action_chunk
def reset_context(context): pass
def release(context): pass
''')
        (root / 'b/main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        (root / 'default_chain').mkdir()
        (root / 'default_chain/policy.yaml').write_text(json.dumps({
            'schema_version': config['schema_version'], 'id': 'default_chain', 'kind': 'chain',
            'policies': ['llm_direction', 'b', 'llm_direction']}))
        host = None
        kwargs = {}
        factory = PolicyProcess
        def worker(timeout=3):
            return factory(root / 'llm_direction', copy.deepcopy(config), init_timeout_s=8,
                           inference_timeout_s=timeout, release_timeout_s=1, **kwargs)
        try:
            yield server, root, config, factory, kwargs, worker
        finally:
            for item in server.responses:
                if item.get('wait'):
                    item['wait'].set()
            if host:
                host.close()


@pytest.fixture
def frames(tmp_path):
    pool = ImagePool(tmp_path / 'frames')
    for name, value in zip(('head', 'left_wrist', 'right_wrist'), (25, 75, 125)):
        pool.publish(name, np.full((12, 16, 3), value, np.uint8), time.monotonic())
    lease = pool.acquire()
    request = {'request_id': 1, 'generation': 1, 'config_version': 1,
               'state': {'qpos': [0] * 14, 'eef': [0] * 14}, 'action_chunk': [],
               'images': lease.descriptors}
    try:
        yield pool, lease, request
    finally:
        lease.release()
        pool.close()


def test_real_worker_reads_three_frames_and_returns_standard_epos(setup, frames):
    server, root, config, factory, kwargs, create = setup
    _, _, request = frames
    worker = create()
    try:
        worker.start()
        assert not server.requests, 'initialize must not make model calls'
        worker.submit(request)
        result = wait(worker.poll)
        assert result['type'] == 'result', result
        assert np.asarray(result['actions']).shape == (3, 14)
        assert result['actions'][-1][2] == pytest.approx(.05)
        assert result['images_released'] is True
        content = server.requests[0]['body']['messages'][1]['content']
        data = [part['image_url']['url'] for part in content if part['type'] == 'image_url']
        for encoded, expected in zip(data, (25, 75, 125)):
            with Image.open(io.BytesIO(base64.b64decode(encoded.split(',')[1]))) as image:
                assert np.max(np.abs(np.asarray(image).astype(int) - expected)) <= 1
    finally:
        worker.close()
    assert not worker.alive


def test_native_aba_preserves_chunk_passthrough_with_three_workers(setup, frames):
    server, root, config, factory, kwargs, _ = setup
    _, _, request = frames
    record = PolicyRegistry(root).get('default_chain')
    worker = ChainProcess(record, process_factory=factory, init_timeout_s=10,
                          inference_timeout_s=4, release_timeout_s=1, **kwargs)
    try:
        worker.start()
        assert len(worker._workers) == 3
        worker.submit(request)
        result = wait(worker.poll)
        assert result['type'] == 'result', result
        # The last instance receives a nonempty upstream chunk and passes it through.
        assert len(server.requests) == 1
        assert result['actions'][-1][2] == pytest.approx(.05)
    finally:
        worker.close()




@pytest.mark.parametrize('hard_timeout', [False, True])
def test_long_http_call_pins_images_until_callback_or_exit_proof(setup, frames, hard_timeout):
    server, _, _, _, _, create = setup
    pool, lease, request = frames
    gate = threading.Event()
    server.responses.append({'wait': gate})
    worker = create(timeout=.3 if hard_timeout else 5)
    extra = []
    try:
        worker.start()
        worker.submit(request)
        assert server.received.wait(5)
        for value in (2, 3):
            pool.publish('head', np.full((12, 16, 3), value, np.uint8), time.monotonic())
            extra.append(pool.acquire())
        assert not pool.publish('head', np.zeros((12, 16, 3), np.uint8), time.monotonic())
        if hard_timeout:
            result = wait(worker.poll)
            assert result['type'] == 'error'
            assert not pool.publish('head', np.zeros((12, 16, 3), np.uint8), time.monotonic())
            worker.close()
            assert not worker.alive
            assert worker.cleanup_confirmed
        else:
            assert not pool.publish('head', np.zeros((12, 16, 3), np.uint8), time.monotonic())
            gate.set()
            result = wait(worker.poll)
            assert result['type'] in ('result', 'cancelled')
            assert result['images_released'] is True
        lease.release()
        assert pool.publish('head', np.zeros((12, 16, 3), np.uint8), time.monotonic())
    finally:
        gate.set()
        worker.close()
        for held in extra:
            held.release()


def make_controller(setup, tmp_path, monkeypatch):
    _, root, _, factory, kwargs, _ = setup
    config = read_config(template_path(ROOT / 'configs/rc.mock.yaml'))
    config.update(policy_directory=str(root), startup_policy='llm_direction',
                  outputs_directory=str(tmp_path / 'outputs'), frequency_hz=50, **kwargs)
    config['robot'] = {'backend': 'mock'}
    config['safety']['enabled'] = False
    config['timeouts'] = {'initialize_s': 10, 'inference_s': 5, 'release_s': 1}
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(config))
    controller = RobotController(path, robot=MockRobot(), process_factory=factory)
    controller.start()
    wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
    return controller


def test_controller_executes_then_pauses_on_done(setup, tmp_path, monkeypatch):
    server = setup[0]
    server.responses.extend([{}, {'body': response_for('deepseek', {'left': 'noop', 'right': 'noop', 'done': True})}])
    controller = make_controller(setup, tmp_path, monkeypatch)
    try:
        controller.submit('resume')
        wait(lambda: len(server.requests) == 2 and controller.status()['state'] == 'paused')
        assert controller.status()['consumed'] == 3
        assert controller.status()['queue_remaining'] == 0
        assert controller._worker.alive
    finally:
        controller.submit('exit')
        assert controller.done.wait(15)


@pytest.mark.parametrize('action', ['pause', 'take', 'set policy.params.task "new task"'])
def test_late_http_decision_cannot_override_pause_take_or_reload(setup, tmp_path, monkeypatch, action):
    server = setup[0]
    gate = threading.Event()
    server.responses.append({'wait': gate})
    controller = make_controller(setup, tmp_path, monkeypatch)
    old = controller._worker
    try:
        controller.submit('resume')
        assert server.received.wait(5)
        operation = controller.submit(action)
        wait(lambda: controller.status()['state'] != 'running')
        gate.set()
        wait(lambda: controller.get_operation(operation)['status'] in ('succeeded', 'failed'))
        assert controller.get_operation(operation)['status'] == 'succeeded'
        wait(lambda: not controller.status()['in_flight'])
        assert controller.status()['consumed'] == 0
        assert controller.status()['queue_remaining'] == 0
        assert controller.status()['control_owner'] == ('mcp' if action == 'take' else 'policy')
        if action.startswith('set'):
            assert controller._worker is not old and not old.alive
            assert controller.status()['state'] == 'paused'
    finally:
        gate.set()
        controller.submit('exit')
        assert controller.done.wait(15)


def test_two_llm_chain_members_keep_distinct_config_keys_in_separate_hosts(setup, frames):
    server, root, config, factory, kwargs, _ = setup
    _, _, request = frames
    first = copy.deepcopy(config)
    first['params'].update(chunk_size=1)
    first['params']['provider']['api_key'] = 'first-policy-key'
    second = copy.deepcopy(config)
    second['id'] = 'b'
    second['params']['request_when_remaining'] = 2
    second['params']['provider']['api_key'] = 'second-policy-key'
    (root / 'llm_direction/policy.yaml').write_text(json.dumps(first))
    (root / 'b/policy.yaml').write_text(json.dumps(second))
    (root / 'b/main.py').write_text((root / 'llm_direction/main.py').read_text())
    (root / 'b/main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    chain_path = root / 'default_chain/policy.yaml'
    chain = read_config(chain_path); chain['policies'] = ['llm_direction', 'b']
    chain_path.write_text(json.dumps(chain))
    record = PolicyRegistry(root).get('default_chain')
    worker = ChainProcess(record, process_factory=factory, init_timeout_s=10,
                          inference_timeout_s=4, release_timeout_s=1, **kwargs)
    try:
        worker.start(); worker.submit(request)
        result = wait(worker.poll)
        assert result['type'] == 'result', result
        assert [r['authorization'] for r in server.requests] == [
            'Bearer first-policy-key', 'Bearer second-policy-key']
    finally:
        worker.close()
