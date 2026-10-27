"""Runtime tools and real local worker transport (not production Conda tests)."""
import sys
import threading
import json
from types import SimpleNamespace

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from rc_policy_sdk.runtime import PolicyRuntime
from host_helpers import LocalPolicyProcess as PolicyProcess
from test_process import wait_response
from test_native_chain_process import wait_for


def runtime(tools=("pause",)):
    value = PolicyRuntime(SimpleNamespace(runtime_tools=frozenset(tools)))
    assert callable(getattr(value, "pause", None)), "PolicyRuntime must offer pause()"
    return value


def test_pause_records_once_without_contacting_rc_and_closes_with_callback():
    value = runtime()
    with value._inference_scope():
        assert value.pause("完成") is None
        value.pause("later reason")
    assert value._control_requests == [{"type": "pause", "reason": "完成"}]
    with pytest.raises(RuntimeError, match="policy.*callback"):
        value.pause()
    # A runtime from initialize/observe never enters an inference scope.
    with pytest.raises(RuntimeError, match="policy.*callback"):
        runtime().pause()


def test_pause_rejects_another_thread_even_while_inference_is_active():
    value = runtime()
    errors = []

    def background():
        try:
            value.pause("background")
        except RuntimeError as exc:
            errors.append(str(exc))

    with value._inference_scope():
        thread = threading.Thread(target=background)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
    assert len(errors) == 1 and "thread" in errors[0]
    assert value._control_requests == []


def test_pause_requires_host_capability_and_valid_reason():
    unsupported = runtime(())
    with unsupported._inference_scope():
        with pytest.raises(RuntimeError, match="upgrade robot-control"):
            unsupported.pause()
    value = runtime()
    with value._inference_scope():
        with pytest.raises(TypeError, match="reason"):
            value.pause(1)
        value.pause()
    assert value._control_requests == [{"type": "pause", "reason": ""}]


@pytest.fixture
def local_worker(tmp_path):
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 3, 3), np.uint8), 1.)
    lease = pool.acquire()
    workers = []

    def create(source, mode="qpos", commands=None):
        directory = tmp_path / f"policy-{len(workers)}"
        directory.mkdir()
        (directory / "main.py").write_text(source)
        (directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        worker = PolicyProcess(directory, {"id": "pause-demo",
                               "execution": {"control_mode": mode}, "tui_commands": commands or {}},
                               python_command=[sys.executable], init_timeout_s=3,
                               inference_timeout_s=3, release_timeout_s=1)
        workers.append(worker)
        request = {"request_id": 1, "generation": 7, "config_version": 1,
                   "state": {"qpos": [1.] * 14, "eef": [2.] * 14},
                   "action_chunk": {"actions": [], "modes": []} if mode == "mixed" else [],
                   "images": lease.descriptors}
        return worker, request

    yield create
    for worker in reversed(workers):
        worker.close()
    lease.release()
    pool.close()


PAUSING_POLICY = '''
def initialize(config, *, runtime):
    return {'calls': 0, 'initial_runtime': runtime}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['calls'] += 1
    if context['calls'] == 1:
        runtime.pause('task done')
        runtime.pause('ignored duplicate')
        context['last_runtime'] = runtime
    else:
        for old in (context['initial_runtime'], context['last_runtime']):
            try:
                old.pause('stale')
            except RuntimeError:
                pass
            else:
                raise AssertionError('old runtime allowed a pause')
    mode = config['execution']['control_mode']
    if mode == 'mixed':
        return {'actions': [state['qpos'], state['eef']], 'modes': ['qpos', 'epos']}
    return [state['eef'] if mode == 'epos' else state['qpos']]
def reset_context(context): pass
def release(context): pass
'''


@pytest.mark.parametrize("mode", ["qpos", "epos", "mixed"])
def test_worker_transmits_pause_with_valid_actions_only_for_that_inference(local_worker, mode):
    worker, request = local_worker(PAUSING_POLICY, mode)
    worker.start()
    worker.submit(request)
    result = wait_response(worker)
    assert result["type"] == "result", result
    assert result["control_requests"] == [{"type": "pause", "reason": "task done"}]
    assert result["actions"] == ([[1.] * 14, [2.] * 14] if mode == "mixed"
                                  else [[2. if mode == "epos" else 1.] * 14])
    worker.submit({**request, "request_id": 2})
    second = wait_response(worker)
    assert second["type"] == "result", second
    assert not second.get("control_requests")
    assert worker.alive


def test_sdk_chain_runs_all_stages_and_merges_pause_requests(local_worker):
    source = '''
from rc_policy_sdk import PolicyChain
class Stage:
    def initialize(self, config): return {'calls': 0}
    def policy(self, context, state, action_chunk, images, config, *, runtime):
        context['calls'] += 1
        if context['calls'] == 1:
            runtime.pause('stage done')
        return [[v + 1 for v in (action_chunk[0] if action_chunk else state['qpos'])]]
    def reset_context(self, context): pass
    def release(self, context): pass
chain = PolicyChain([Stage(), PolicyChain([Stage(), Stage()])])
def initialize(config, *, runtime): return chain.initialize(config, runtime=runtime)
def policy(context, state, action_chunk, images, config, *, runtime):
    return chain.policy(context, state, action_chunk, images, config, runtime=runtime)
def reset_context(context): chain.reset_context(context)
def release(context): chain.release(context)
'''
    worker, request = local_worker(source)
    worker.start()
    worker.submit(request)
    result = wait_response(worker)
    assert result["type"] == "result", result
    assert result["actions"] == [[4.] * 14]
    assert result["control_requests"] == [{"type": "pause", "reason": "stage done"}]
    worker.submit({**request, "request_id": 2})
    assert not wait_response(worker).get("control_requests")


def test_initialize_observe_and_parallel_command_cannot_request_pause(local_worker):
    source = '''
import threading
def rejected(runtime):
    try:
        runtime.pause('outside inference')
    except RuntimeError:
        return True
    raise AssertionError('pause allowed outside its callback thread')
def initialize(config, *, runtime):
    assert rejected(runtime)
    return {'entered': threading.Event(), 'finish': threading.Event()}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['active_runtime'] = runtime
    runtime.pause('valid inference')
    context['entered'].set()
    if not context['finish'].wait(2):
        raise AssertionError('command did not complete while inference was active')
    return [state['qpos']]
def observe(context, state, images, config, *, runtime):
    assert context['entered'].wait(1)
    assert rejected(runtime)
    assert rejected(context['active_runtime'])
    context['observed'] = True
def probe(context):
    assert context['observed']
    assert rejected(context['active_runtime'])
    context['finish'].set()
    return 'command rejected pause'
def reset_context(context): pass
def release(context): pass
'''
    worker, request = local_worker(source, commands={'probe': {'handler': 'probe', 'args': [], 'timeout_s': 1}})
    worker.start()
    wait_for(lambda: worker.supports_observe)
    worker.submit(request)
    assert worker.observe({**request, 'request_id': 2})
    assert wait_for(worker.poll_observation)['type'] == 'observed'
    worker.submit_command('probe-1', 'probe', [])
    response = wait_for(worker.poll_command)
    if response['type'] == 'command_started':
        response = wait_for(worker.poll_command)
    assert response['result'] == 'command rejected pause'
    result = wait_response(worker)
    assert result['type'] == 'result', result
    assert result['control_requests'] == [{'type': 'pause', 'reason': 'valid inference'}]


def test_pause_against_older_host_fails_explicitly(local_worker, monkeypatch):
    worker, request = local_worker(PAUSING_POLICY)
    from robot_control.host_client import HostClient
    send_request = HostClient.request

    def old_host(self, kind, **kwargs):
        if kind == 'load':
            kwargs['event'] = {key: value for key, value in kwargs['event'].items() if key != 'runtime_tools'}
        return send_request(self, kind, **kwargs)

    monkeypatch.setattr(HostClient, "request", old_host)
    worker.start()
    worker.submit(request)
    result = wait_response(worker)
    assert result["type"] == "error"
    assert "upgrade robot-control" in result["error"]


@pytest.mark.parametrize("ending", ["return None", "return []", "raise ValueError('model failed')"])
def test_pause_does_not_hide_bad_output_or_callback_failure(local_worker, ending):
    source = f'''
def initialize(config): return None
def policy(context, state, action_chunk, images, config, *, runtime):
    runtime.pause('done')
    {ending}
def reset_context(context): pass
def release(context): pass
'''
    worker, request = local_worker(source)
    worker.start()
    worker.submit(request)
    result = wait_response(worker)
    assert result["type"] == "error"
    assert "pause" not in result["error"], result
    assert not result.get("control_requests")


@pytest.mark.parametrize("controls,tools,expected", [
    (None, [], "result"),  # Older SDK: no capability or result metadata.
    ([], ["pause"], "result"),
    ([{"type": "pause", "reason": "ok"}], ["pause"], "result"),
    ([{"type": "pause", "reason": "ok"}], [], "error"),
    ({"type": "pause", "reason": "bad container"}, ["pause"], "error"),
    ([{"type": "resume", "reason": "unsupported"}], ["pause"], "error"),
    ([{"type": "pause"}], ["pause"], "error"),
    ([{"type": "pause", "reason": 5}], ["pause"], "error"),
    ([{"type": "pause", "reason": "one"}] * 2, ["pause"], "error"),
    ([{"type": "pause", "reason": "ok", "source": {"policy_id": "spoof", "cell": 1}}], ["pause"], "error"),
])
def test_supervisor_validates_tool_results_and_accepts_legacy_workers(local_worker, monkeypatch, controls, tools, expected):
    worker, request = local_worker('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return [[0.] * 14]
def reset_context(context): pass
def release(context): pass
''')
    receive = worker._receive

    def raw_metadata(packet):
        # Alter decoded metadata at the actual HostClient consumer boundary.
        # The real host still owns execution, identities and exit proof.
        event = dict(packet['event'])
        if event['type'] == 'ready':
            event['runtime_tools'] = tools
        elif event['type'] == 'result':
            event.pop('control_requests', None)
            if controls is not None:
                event['control_requests'] = controls
        receive({**packet, 'event': event})

    monkeypatch.setattr(worker, '_receive', raw_metadata)
    worker.start()
    worker.submit(request)
    result = wait_response(worker)
    assert result["type"] == expected, result
    if expected == "error":
        assert "control_requests" in result["error"] or "runtime_tools" in result["error"]
