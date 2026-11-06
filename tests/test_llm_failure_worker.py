"""Actual policy entry, SDK workers, POSIX frames and controller/MCP integration."""
from config_helpers import read_config
from itertools import count
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
from test_host_transport import wait_for
from rc_policy_sdk.images import ImagePool
from robot_control.controller import RobotController
from robot_control.global_state import GlobalStateService
from robot_control.mcp_server import MCPService
from robot_control.robots import MockRobot


NATIVE_MODES = ('choice', 'predicate', 'score')


def profile_path(mode):
    return 'provider.yaml' if mode == 'deepseek' else f'providers/gpt-decision-{mode}.yaml'


def decision_body(answer):
    return {'model': 'gpt-test', 'answers': [answer],
            'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11,
                      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}


def native_answer(mode, failed):
    if mode == 'choice':
        choice = 'failed' if failed else 'not_failed'
        return {'type': 'choice', 'name': 'failed', 'choice': choice, 'confidence': 1.,
                'probabilities': [{'value': value, 'probability': float(value == choice)}
                                  for value in ('not_failed', 'failed')]}
    if mode == 'predicate':
        return {'type': 'predicate', 'name': 'failed', 'probability': .8 if failed else .799}
    probabilities = (0., .5, .5) if failed else (0., 1., 0.)
    return {'type': 'score', 'name': 'severity', 'score': 1.5 if failed else 1., 'confidence': max(probabilities),
            'probabilities': [{'value': value, 'label': label, 'probability': probabilities[value]}
                              for value, label in enumerate(('正常', '疑似偏离', '明确失败'))]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    root = tmp_path / 'policy'
    root.mkdir()
    with model_server() as server:
        def create(mode='deepseek', **settings):
            assert (ROOT / 'policy/llm_failure/main.py').is_file(), 'llm_failure entry point is missing'
            folder = root / 'llm_failure'
            copy_templates(ROOT / 'policy/llm_failure', folder)
            config = read_config(folder / 'policy.yaml')
            config['params']['provider_config_path'] = profile_path(mode)
            profile = folder / config['params']['provider_config_path']
            assert profile.is_file(), f'missing usable profile for {mode}: {profile.name}'
            profile.write_text(json.dumps({**read_config(profile), 'base_url': server.url,
                                           'task': 'put the block in the tray', 'timeout_s': 4.,
                                           'api_key': 'local-test-key', **settings}))
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
    return wait_for(worker.poll)


@pytest.mark.parametrize('mode', ['deepseek', 'choice'])
@pytest.mark.parametrize('forced_close', [False, True])
def test_worker_releases_frames_while_http_is_pending(setup, frames, forced_close, mode):
    server, _, create = setup
    folder, config = create(mode)
    _, _, request = frames
    gate = threading.Event()
    body = decision_body(native_answer(mode, True)) if mode == 'choice' else response_for('deepseek', {'failed': True})
    server.responses.append({'wait': gate, 'body': body})
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
            finish, = received['control_requests']
            assert (finish['type'], finish['outcome']) == ('finish', 'terminated')
            assert ('GPT' if mode == 'choice' else 'DeepSeek') in finish['message']
            assert server.requests[0]['path'] == ('/v1/decisions' if mode == 'choice' else '/v1/chat/completions')
    finally:
        gate.set()
        worker.close()


@pytest.mark.parametrize('mode', NATIVE_MODES)
def test_worker_counts_recoverable_decision_failures_and_terminates_on_valid_answer(setup, frames, mode):
    server, _, create = setup
    folder, config = create(mode)
    _, _, request = frames
    good = native_answer(mode, True)
    checks = [
        ({'body': decision_body(native_answer(mode, False))}, 0),
        ({'body': decision_body({'type': 'refusal', 'name': good['name']})}, 1),
        ({'status': 503, 'body': {'error': {'message': 'temporary failure'}}}, 2),
        ({'body': decision_body(good)}, 2),
    ]
    worker = LocalPolicyProcess(folder, config, init_timeout_s=8, release_timeout_s=2)
    request_ids = count(1)

    def infer(consumed):
        result = response(worker, {**request, 'request_id': next(request_ids),
                                   'runtime': {'control_epoch': 1, 'consumed_actions': consumed}})
        assert result['type'] == 'result', result
        assert result['images_released'] and result['actions'] == request['action_chunk']
        return result.get('control_requests', [])

    def completed_metrics():
        assert worker.submit_monitor(1)
        reply = wait_for(worker.poll_monitor)
        assert reply['type'] == 'monitor_result', reply
        values = reply['result']
        return values if values['in_flight'] == 0 else None

    try:
        worker.start()
        assert not infer(100)
        for index, (http_reply, failures) in enumerate(checks, 1):
            server.responses.append(http_reply)
            due = 100 + index * 60
            assert not infer(due)
            wait(lambda: len(server.requests) == index)
            metrics = wait_for(completed_metrics)
            assert metrics['failed_requests'] == failures
            assert metrics['avg_total_tokens'] == metrics['max_total_tokens'] == 11
            assert 0 <= metrics['avg_http_s'] <= metrics['max_http_s']
            if index < len(checks):
                assert not infer(due)
            else:
                finish, = wait_for(lambda: infer(due))
                assert (finish['type'], finish['outcome']) == ('finish', 'terminated')
        assert len(server.requests) == 4
    finally:
        worker.close()


@pytest.mark.parametrize('mode', ['deepseek', 'predicate'])
def test_worker_uses_globals_and_robot_state_from_each_image_sample(setup, frames, tmp_path, mode):
    server, _, create = setup
    templates = {'task': 'Task {task}', 'history_snapshot': 'H|{step}|{$stage}|{$qpos}|{$epos}',
                 'current_snapshot': 'C|{step}|{$stage}|{$qpos}|{$epos}', 'camera': 'Camera {camera}'}
    folder, config = create(mode, input_templates=templates)
    _, _, request = frames
    if mode == 'deepseek':
        body = response_for(mode, {'failed': False})
    else:
        body = decision_body(native_answer(mode, False))
    server.responses.append({'body': body})
    service = GlobalStateService(tmp_path / 'globals.env', tmp_path / 'ipc', run_id='failure-worker-test')
    service.start()
    worker = None
    try:
        worker = LocalPolicyProcess(folder, config, global_state=service, init_timeout_s=8, release_timeout_s=2)
        worker.start()
        service.proxy['stage'] = 1
        first = {'qpos': [1.] * 14, 'eef': [2.] * 14}
        assert response(worker, {**request, 'state': first})['actions'] == request['action_chunk']
        service.proxy['stage'] = 2
        second = {'qpos': [3.] * 14, 'eef': [4.] * 14}
        due = {**request, 'state': second, 'request_id': 2, 'runtime': {'consumed_actions': 160, 'control_epoch': 1}}
        assert response(worker, due)['actions'] == request['action_chunk']
        assert server.received.wait(3)
        body = server.requests[0]['body']
        content = body['messages'][1]['content'] if mode == 'deepseek' else body['input'][0]['content']
        snapshots = [item['text'].split('|') for item in content if item.get('text', '').startswith(('H|', 'C|'))]
        assert [(kind, step, stage, json.loads(qpos), json.loads(epos))
                for kind, step, stage, qpos, epos in snapshots] == [
                    ('H', '0', '1', first['qpos'], first['eef']),
                    ('C', '60', '2', second['qpos'], second['eef'])]
    finally:
        if worker is not None:
            worker.close()
        service.close(time.monotonic() + 3)


@pytest.mark.parametrize('verdict,waiting,switch_mode', [
    (True, True, None), (True, False, 'choice'), (True, True, 'predicate'), (True, True, 'score'),
    (False, True, None), ('invalid', True, None), ('tail_failure', True, None)])
def test_real_chain_async_check_and_mcp_ownership(setup, tmp_path, verdict, waiting, switch_mode):
    server, root, create = setup
    folder, _ = create()
    if switch_mode:
        alternate = folder / profile_path(switch_mode)
        assert alternate.is_file(), f'missing switch target: {alternate.name}'
        alternate.write_text(json.dumps({**read_config(alternate), 'base_url': server.url,
                                         'model': 'new-failure-model', 'api_key': 'alternate-failure-key',
                                         'task': 'inspect alternate tray', 'prompt': 'alternate judgement'}))
        # This is a usable pre-change file, including its private settings.
        legacy = {**read_config(folder / 'provider.yaml'), 'type': 'openai_decisions',
                  'choice_descriptions': {'failed': 'failure', 'not_failed': 'ongoing'}}
        (folder / 'providers/legacy.yaml').write_text(json.dumps(legacy))
        mixed = {**read_config(alternate), 'choice_descriptions': legacy['choice_descriptions']}
        (folder / 'providers/mixed.yaml').write_text(json.dumps(mixed))
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
def switch_provider(context, path): return implementation.switch_provider(context, path)
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
    if switch_mode:
        body = decision_body(native_answer(switch_mode, True))
    server.responses.append({'wait': gate, 'body': body})
    server.responses.extend({'body': response_for('deepseek', {'failed': False})} for _ in range(20))
    controller = RobotController(path, robot=MockRobot(), process_factory=LocalPolicyProcess)
    service = MCPService(controller, {'port': 0}, allow_ephemeral=True)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'), 12)
        assert controller.status()['state'] == 'paused', controller.read_events()
        if switch_mode:
            from test_chain_controller import operation
            chain = controller._worker
            previous = chain.member_workers()
            switch = 'policy llm_failure switch-provider '
            receipt = operation(controller, controller.submit(switch + profile_path(switch_mode)))
            assert previous['llm_failure'].cleanup_confirmed
            active = chain.member_workers()
            assert active['llm_failure'] is not previous['llm_failure']
            assert active['producer'] is previous['producer']
            for bad in ('providers/legacy.yaml', 'providers/mixed.yaml'):
                rejected = operation(controller, controller.submit(switch + bad), 'failed')
                assert 'alternate-failure-key' not in str(rejected)
                assert controller._worker is chain and chain.runnable
                assert active == chain.member_workers()
            assert controller.status()['state'] == 'paused'
            assert 'alternate-failure-key' not in str(receipt) and not server.requests
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
        if switch_mode:
            request = server.requests[0]
            assert 'inspect alternate tray' in request['body']['input'][0]['content'][0]['text']
            question, = request['body']['questions']
            assert question['type'] == switch_mode
            assert question['instructions'].startswith('alternate judgement\n\n')
            assert (request['path'], request['body']['model'], request['authorization']) == (
                '/v1/decisions', 'new-failure-model', 'Bearer alternate-failure-key')
        gate.set()
        if verdict is False or verdict == 'invalid':
            wait(lambda: len(server.requests) >= 2)
            assert controller.get_policy_result(identity)['status'] == 'running'
            assert controller.status()['state'] == 'running'
            assert controller.status()['control_owner'] == 'policy'
            assert worker.runnable
            controller.submit('pause')
        elif verdict == 'tail_failure':
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


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['deepseek', *NATIVE_MODES])
async def test_real_controller_registers_instance_widget_with_six_monitor_values(setup, tmp_path, mode):
    server, root, create = setup
    create(mode)
    path = tmp_path / 'rc-monitor.yaml'
    path.write_text(json.dumps({'schema_version': 3, 'frequency_hz': 50,
        'policy_directory': 'policy', 'startup_policy': 'llm_failure',
        'robot': {'backend': 'mock'}, 'safety': {'enabled': False, 'reference_frequency_hz': 50,
        'max_delta_per_step': [.1] * 14}, 'reset_qpos': [0.] * 14, 'outputs_directory': 'outputs',
        'timeouts': {'initialize_s': 10, 'inference_s': 5, 'release_s': 2}}))
    controller = RobotController(path, robot=MockRobot(), process_factory=LocalPolicyProcess)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'), 12)
        assert controller.status()['state'] == 'paused', controller.read_events()
        assert controller._worker.supports_monitor
        from robot_control.graphics import GraphicsSupport
        from robot_control.tui import CommandInput, RobotControlApp
        from robot_control.policy_monitor_tui import PolicyMonitorPanel
        from test_policy_manager_tui import eventually
        app = RobotControlApp(controller, start_controller=False, graphics=GraphicsSupport('kitty'))
        async with app.run_test(size=(110, 36)) as pilot:
            field = app.query_one(CommandInput)
            field.value = 'monitor '
            field.cursor_position = len(field.value)
            await eventually(pilot, lambda: field.completion_result is not None and any(
                item.label == 'llm_failure' for item in field.completion_result.items))
            field.value = ''
            app.router.submit('monitor llm_failure')
            await eventually(pilot, lambda: controller.policy_monitors.snapshot().get('llm_failure', {}).get('values'))
            assert controller.policy_monitors.snapshot()['llm_failure']['values'] == {
                'in_flight': 0, 'avg_http_s': '—', 'max_http_s': '—',
                'avg_total_tokens': '—', 'max_total_tokens': '—', 'failed_requests': 0}
            assert len(list(app.query(PolicyMonitorPanel))) == 1
        assert controller.status()['state'] == 'paused'
        assert not server.requests
    finally:
        controller.submit('exit')
        assert controller.done.wait(12)
