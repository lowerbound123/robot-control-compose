"""Task results are deferred metadata, never a second robot control path."""
import threading
from types import SimpleNamespace

import pytest

from rc_policy_sdk.runtime import PolicyRuntime
from rc_policy_sdk.control import control_requests
from test_runtime_pause import local_worker as local_worker
from test_process import wait_response


def runtime(tools=('pause', 'finish')):
    return PolicyRuntime(SimpleNamespace(runtime_tools=frozenset(tools)))


def test_finish_is_callback_scoped_and_keeps_first_result():
    value = runtime()
    with pytest.raises(RuntimeError, match='callback'):
        value.finish()
    with value._inference_scope():
        assert value.finish('任务完成') is None
        value.finish('later', outcome='terminated')
    assert value._control_requests == [{'type': 'finish', 'outcome': 'completed', 'message': '任务完成'}]
    with pytest.raises(RuntimeError, match='callback'):
        value.finish()


def test_finish_rejects_background_threads_and_unnegotiated_hosts():
    value = runtime()
    errors = []
    def background():
        try:
            value.finish()
        except RuntimeError as exc:
            errors.append(str(exc))
    with value._inference_scope():
        thread = threading.Thread(target=background)
        thread.start()
        thread.join(1)
    assert len(errors) == 1 and 'thread' in errors[0]
    value = runtime(('pause',))
    with value._inference_scope(), pytest.raises(RuntimeError, match='upgrade'):
        value.finish()


@pytest.mark.parametrize('kwargs,error', [({'message': 1}, TypeError), ({'outcome': 'unknown'}, ValueError)])
def test_finish_rejects_invalid_result(kwargs, error):
    value = runtime()
    with value._inference_scope(), pytest.raises(error):
        value.finish(**kwargs)
    assert value._control_requests == []


@pytest.mark.parametrize('pause_first', [False, True])
def test_pause_overrides_finish_regardless_of_call_order(pause_first):
    value = runtime()
    with value._inference_scope():
        if pause_first:
            value.pause('operator')
        value.finish('done')
        value.pause('operator')
        value.finish('later')
    assert value._control_requests == [{'type': 'pause', 'reason': 'operator'}]


@pytest.mark.parametrize('invalid', [
    {'type': 'finish', 'outcome': 'other', 'message': ''},
    {'type': 'finish', 'outcome': 'completed', 'message': 2},
    {'type': 'finish', 'outcome': 'completed'},
    {'type': 'finish', 'outcome': 'completed', 'message': '', 'source': {'policy_id': 'spoof', 'cell': 1}},
])
def test_wire_rejects_invalid_or_forged_finish(invalid):
    with pytest.raises(ValueError):
        control_requests([invalid])


@pytest.mark.parametrize('mode', ['qpos', 'epos', 'mixed'])
def test_real_worker_transmits_finish_once_after_valid_actions(local_worker, mode):
    worker, request = local_worker('''
def initialize(config): return {'calls': 0}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['calls'] += 1
    if context['calls'] == 1:
        runtime.finish('请检查抓取', outcome='terminated')
    assert not images['head'].flags.writeable
    return {'actions': [state['qpos']], 'modes': ['qpos']} if config['execution']['control_mode'] == 'mixed' else [state['qpos']]
def reset_context(context): pass
def release(context): pass
''', mode=mode)
    worker.start()
    worker.submit(request)
    response = wait_response(worker)
    assert response['type'] == 'result', response
    assert response['images_released'] is True
    assert response['control_requests'] == [{'type': 'finish', 'outcome': 'terminated', 'message': '请检查抓取'}]
    worker.submit({**request, 'request_id': 2})
    assert not wait_response(worker).get('control_requests')


def test_sdk_nested_chain_keeps_all_stages_and_pause_priority(local_worker):
    worker, request = local_worker('''
from rc_policy_sdk import PolicyChain
class Stage:
    def __init__(self, pause=False): self.pause = pause
    def initialize(self, config): return None
    def policy(self, context, state, action_chunk, images, config, *, runtime):
        runtime.pause('interrupted') if self.pause else runtime.finish('done')
        return [[v + 1 for v in (action_chunk[0] if action_chunk else state['qpos'])]]
    def reset_context(self, context): pass
    def release(self, context): pass
chain = PolicyChain([Stage(), PolicyChain([Stage(True), Stage()])])
def initialize(config): return chain.initialize(config)
def policy(context, state, action_chunk, images, config, *, runtime):
    return chain.policy(context, state, action_chunk, images, config, runtime=runtime)
def reset_context(context): chain.reset_context(context)
def release(context): chain.release(context)
''')
    worker.start()
    worker.submit(request)
    response = wait_response(worker)
    assert response['type'] == 'result', response
    assert response['actions'] == [[4.] * 14]
    assert response['control_requests'] == [{'type': 'pause', 'reason': 'interrupted'}]


def test_finish_requires_negotiated_worker_capability(local_worker, monkeypatch):
    worker, request = local_worker('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0.] * 14]
def reset_context(context): pass
def release(context): pass
''')
    receive = worker._receive
    def unnegotiated(packet):
        event = dict(packet['event'])
        if event['type'] == 'ready':
            event['runtime_tools'] = ['pause']
        elif event['type'] == 'result':
            event['control_requests'] = [{'type': 'finish', 'outcome': 'completed', 'message': ''}]
        receive({**packet, 'event': event})
    monkeypatch.setattr(worker, '_receive', unnegotiated)
    worker.start()
    worker.submit(request)
    response = wait_response(worker)
    assert response['type'] == 'error'
    assert 'finish' in response['error']
