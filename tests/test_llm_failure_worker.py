"""Actual policy entry, SDK workers, POSIX frames and controller/MCP integration."""
from config_helpers import read_config
import json
import threading
import time

import numpy as np
import pytest

pytest.importorskip('openai', reason='real LLM workers require the optional LLM environment')
from llm_failure_helpers import ROOT, wait
from llm_http_helpers import model_server, response_for
from project_templates import copy_templates
from host_helpers import LocalPolicyProcess
from rc_policy_sdk.images import ImagePool
from robot_control.controller import RobotController
from robot_control.mcp_server import MCPService
from robot_control.robots import MockRobot


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    root = tmp_path / 'policy'
    root.mkdir()
    with model_server() as server:
        def create():
            assert (ROOT / 'policy/llm_failure/main.py').is_file(), 'llm_failure entry point is missing'
            folder = root / 'llm_failure'
            copy_templates(ROOT / 'policy/llm_failure', folder)
            config = read_config(folder / 'policy.yaml')
            config['params']['task'] = 'put the block in the tray'
            config['params']['provider'].update(base_url=server.url, timeout_s=4., api_key='local-test-key')
            (folder / 'policy.yaml').write_text(json.dumps(config))
            return folder, config
        try:
            yield server, root, create
        finally:
            for response in server.responses:
                if response.get('wait'):
                    response['wait'].set()


@pytest.fixture
def frames():
    pool = ImagePool()
    for name in ('head', 'left_wrist', 'right_wrist'):
        pool.publish(name, np.zeros((8, 12, 3), np.uint8), time.monotonic())
    lease = pool.acquire()
    request = {'request_id': 1, 'generation': 1, 'config_version': 1,
               'state': {'qpos': [0.] * 14}, 'action_chunk': [[.125] * 14] * 3,
               'images': lease.descriptors, 'runtime': {'consumed_actions': 100, 'control_epoch': 1}}
    try:
        yield pool, lease, request
    finally:
        lease.release()
        pool.close()


def response(worker, request):
    worker.submit(request)
    result = []
    def collect():
        item = worker.poll()
        if item is not None:
            result.append(item)
        return result
    wait(collect)
    return result[0]


@pytest.mark.parametrize('forced_close', [False, True])
def test_worker_releases_frames_while_http_is_pending(setup, frames, forced_close):
    server, _, create = setup
    folder, config = create()
    _, _, request = frames
    gate = threading.Event()
    server.responses.append({'wait': gate, 'body': response_for('deepseek', {'failed': True})})
    worker = LocalPolicyProcess(folder, config, init_timeout_s=8, inference_timeout_s=3,
                                release_timeout_s=.2 if forced_close else 2)
    try:
        worker.start()
        initial = response(worker, request)
        assert initial['type'] == 'result' and not server.requests
        before_due = response(worker, {**request, 'request_id': 2,
                                      'runtime': {'consumed_actions': 159, 'control_epoch': 1}})
        assert before_due['type'] == 'result'
        assert not server.received.wait(.05)
        request = {**request, 'request_id': 3, 'runtime': {'consumed_actions': 160, 'control_epoch': 1}}
        pending = response(worker, request)
        assert server.received.wait(3)
        assert pending['type'] == 'result', pending
        assert pending['images_released'] is True
        assert pending['actions'] == request['action_chunk']
        assert not pending.get('control_requests')
        if forced_close:
            assert worker.close() is True
            assert worker.cleanup_confirmed
        else:
            gate.set()
            for serial in range(4, 30):
                received = response(worker, {**request, 'request_id': serial})
                assert received['type'] == 'result', received
                if received.get('control_requests'):
                    break
                time.sleep(.01)
            assert received['control_requests'] == [{'type': 'finish', 'outcome': 'terminated',
                'message': 'llm_failure：DeepSeek 判断任务已进入失败状态'}]
    finally:
        gate.set()
        worker.close()


@pytest.mark.parametrize('verdict,waiting', [(True, True), (True, False), (False, True), ('invalid', True), ('tail_failure', True)])
def test_real_chain_async_check_and_mcp_ownership(setup, tmp_path, verdict, waiting):
    server, root, create = setup
    create()
    producer = root / 'producer'
    producer.mkdir()
    (producer / 'main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [state['qpos']] * 180
def reset_context(context): pass
def release(context): pass
''')
    (producer / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    (producer / 'policy.yaml').write_text(json.dumps({'schema_version': 3, 'id': 'producer',
        'execution': {'mode': 'async', 'call_every_actions': 1, 'control_mode': 'qpos'}, 'params': {}}))
    order = ['producer', 'llm_failure']
    marker = tmp_path / 'finish-requested'
    if verdict == 'tail_failure':
        # Observe the actual production callback's effect at the SDK boundary;
        # do not add test hooks or command handlers to the production policy.
        folder = root / 'llm_failure'
        (folder / 'implementation.py').write_text((folder / 'main.py').read_text())
        (folder / 'main.py').write_text(f'''
from pathlib import Path
from . import implementation
def initialize(config): return implementation.initialize(config)
def policy(context, state, action_chunk, images, config, *, runtime):
    result = implementation.policy(context, state, action_chunk, images, config, runtime=runtime)
    if runtime._control_requests: Path({str(marker)!r}).touch()
    return result
def reset_context(context): implementation.reset_context(context)
def release(context): implementation.release(context)
''')
        tail = root / 'tail'
        tail.mkdir()
        (tail / 'main.py').write_text(f'''
from pathlib import Path
def initialize(config): return None
def policy(context, state, action_chunk, images, config):
    if Path({str(marker)!r}).exists(): raise RuntimeError('tail stage failed after finish')
    return action_chunk
def reset_context(context): pass
def release(context): pass
''')
        (tail / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
        (tail / 'policy.yaml').write_text(json.dumps({'schema_version': 3, 'id': 'tail',
            'execution': {'mode': 'async', 'call_every_actions': 1, 'control_mode': 'qpos'}, 'params': {}}))
        order.append('tail')
    (root / 'chain').mkdir()
    (root / 'chain/policy.yaml').write_text(json.dumps({'schema_version': 3, 'kind': 'chain',
                                                     'policies': order}))
    rc = {'schema_version': 3, 'frequency_hz': 100, 'policy_directory': 'policy', 'startup_policy': 'chain',
          'robot': {'backend': 'mock'}, 'safety': {'enabled': False, 'reference_frequency_hz': 50,
          'max_delta_per_step': [.1] * 14}, 'reset_qpos': [0] * 14, 'outputs_directory': 'outputs',
          'timeouts': {'initialize_s': 10, 'inference_s': 5, 'release_s': 2}}
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(rc))
    gate = threading.Event()
    body = response_for('deepseek', {'failed': True if verdict == 'tail_failure' else verdict})
    server.responses.append({'wait': gate, 'body': body})
    server.responses.extend({'body': response_for('deepseek', {'failed': False})} for _ in range(20))
    controller = RobotController(path, robot=MockRobot(), process_factory=LocalPolicyProcess)
    service = MCPService(controller, {'port': 0}, allow_ephemeral=True)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'), 12)
        assert controller.status()['state'] == 'paused', controller.read_events()
        worker = controller._worker
        service.start()
        if waiting:
            assert not service.submit_tool('take', {}).result(3).is_error
            receipt = service.submit_tool('return_and_wait', {'timeout_s': 0}).result(3).structured_content
            identity = receipt['operation_id']
        else:
            controller.submit('resume')
        assert server.received.wait(5)
        before = controller.status()['consumed']
        assert before >= 60
        wait(lambda: controller.status()['consumed'] > before + 3)
        assert controller.status()['control_owner'] == 'policy'
        assert len(server.requests) == 1
        gate.set()
        if verdict is False:
            wait(lambda: len(server.requests) >= 2)
            assert controller.get_policy_result(identity)['status'] == 'running'
            assert controller.status()['state'] == 'running'
            controller.submit('pause')
        elif verdict in ('invalid', 'tail_failure'):
            wait(lambda: controller.get_policy_result(identity)['finished'])
            assert controller.get_policy_result(identity)['status'] == 'failed'
            if verdict == 'tail_failure':
                assert marker.exists()
                assert 'tail stage failed after finish' in controller.get_policy_result(identity)['reason']
            assert controller.status()['control_owner'] == 'mcp'
            wait(lambda: worker.cleanup_confirmed, 8)
        else:
            wait(lambda: controller.status()['state'] == 'paused')
            assert controller.status()['control_owner'] == ('mcp' if waiting else 'policy')
            if waiting:
                result = service.submit_tool('get_policy_result', {}).result(3).structured_content
                assert result['status'] == 'succeeded' and result['result']['outcome'] == 'terminated'
                assert result['result']['source'] == {'policy_id': 'llm_failure', 'cell': 2}
            assert worker.runnable
    finally:
        gate.set()
        service.close()
        controller.submit('exit')
        assert controller.done.wait(12)
