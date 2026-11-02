import threading
import time
from contextlib import contextmanager

import pytest

from robot_control.global_state import GlobalStateService
from robot_control.external import ExternalResources
from robot_control.process import PolicyProcess


class Memory:
    descriptor = {'store_id':'store'}
    @contextmanager
    def locked(self, **kwargs): yield
    def publish(self, version, raw): pass


def test_queued_global_write_cannot_cross_context(tmp_path):
    service = GlobalStateService(tmp_path/'state', tmp_path, run_id='run')
    service.memory = Memory()
    service.accepting = True
    worker = type('Worker', (), {'session_id':'s', 'expected_host':None, 'context_id':'old'})()
    old = service.attach_session(worker)['binding']
    replies = []
    service.receive(worker, dict(binding=old, rpc_id=1, values={'bad':1}), replies.append)
    worker.context_id = 'new'
    service.attach_session(worker)
    service.thread = threading.Thread(target=service._run)
    service.thread.start()
    try:
        deadline = time.monotonic() + 1
        while not replies and time.monotonic() < deadline: time.sleep(.005)
        assert 'error' in replies[0]
        assert service.snapshot()[1] == {}
    finally:
        service.queue.put(None)
        service.thread.join()


def test_external_reopen_rejects_old_context(tmp_path):
    worker = type('Worker', (), {'context_id':'old'})()
    resources = ExternalResources(None, tmp_path/'recovery')
    resources.begin_close(worker)
    resources.close_worker(worker)
    worker.context_id = 'new'
    resources.reopen_context(worker)
    with pytest.raises(RuntimeError, match='context'):
        resources.handle(worker, dict(method='register', remote_id='x', endpoint='http://localhost', owner_id='a', model_id='m', context_id='old'))
    resources.handle(worker, dict(method='register', remote_id='x', endpoint='http://localhost', owner_id='a', model_id='m', context_id='new'))


def test_reinitialization_operations_use_context_and_explicit_budgets(tmp_path):
    config = {'id':'a', 'params':{}, 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused', inference_timeout_s=5, release_timeout_s=7, init_timeout_s=11)
    worker._state = 'running'
    worker.context_id = 'old'
    worker.supports_commands = True
    events = []
    def enqueue(event):
        events.append(event)
        if 'operation_id' in event:
            worker._acks[event['operation_id']] = dict(type='ack', operation_id=event['operation_id'], config_version=event['config_version'], context_id=event['context_id'], observe=True, monitor=False)
    worker._enqueue = enqueue
    candidate = dict(config, params={'x':1})
    worker.validate_reinitialize(candidate, 2, None)
    worker.release_context(None)
    flags = worker.initialize_context(candidate, 2, None)
    assert [e['type'] for e in events if 'operation_id' in e] == ['reinit_validate','reinit_release','reinit_initialize']
    assert events[-1]['context_id'] != 'old'
    assert worker.config_version == 1
    worker.commit_reinitialize(candidate, 2, flags)
    assert worker.config_version == 2 and worker.supports_observe and not worker.supports_monitor


def test_cancel_before_release_preserves_old_binding(tmp_path):
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused')
    worker._state = 'running'
    worker.context_id = 'old'
    worker.global_state = type('Service', (), {'revoke': lambda self, owner: setattr(self, 'revoked', True)})()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match='cancelled'):
        worker.release_context(cancel)
    assert worker._context_enabled
    assert not getattr(worker.global_state, 'revoked', False)


def test_real_host_reinitializes_same_pid_with_global_state(tmp_path):
    from test_host_transport import Host
    directory = tmp_path / 'policies' / 'a'
    directory.mkdir(parents=True)
    (directory / 'main.py').write_text('''
def initialize(config, *, runtime):
    runtime.globals["initialized"] = config["params"]["value"]
    return runtime.globals
def validate_config(config): pass
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): context["released"] = True
def reload(context): return None
''')
    (directory / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    service = GlobalStateService(tmp_path/'global.env', tmp_path/'ipc', run_id='run')
    service.start()
    host = Host(directory.parent, 'a')
    config = dict(schema_version=3, id='a', params={'value':1}, execution={'mode':'sync', 'call_every_actions':1, 'control_mode':'qpos'}, hot_reloadable=[], tui_commands={'reload': {'handler':'reload','args':[], 'effect':'reinitialize_policy'}})
    worker = PolicyProcess(directory, config, host_socket=host.socket, global_state=service, init_timeout_s=3, release_timeout_s=3)
    try:
        worker.start()
        old_pid, old_context = worker.pid, worker.context_id
        candidate = {**config, 'params':{'value':2}}
        worker.validate_reinitialize(candidate, 2, None)
        worker.release_context(None)
        assert service.proxy['released'] is True
        flags = worker.initialize_context(candidate, 2, None)
        worker.commit_reinitialize(candidate, 2, flags)
        assert worker.resume_commands()
        assert worker.pid == old_pid and worker.context_id != old_context
        assert service.proxy['initialized'] == 2
    finally:
        worker.close()
        host.close()
        service.close(time.monotonic()+3)


def test_context_transition_blocks_monitor_and_early_resume(tmp_path):
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused')
    worker._state = 'running'
    worker.context_id = 'old'
    worker.supports_commands = worker.supports_monitor = True
    def enqueue(event):
        if 'operation_id' in event:
            worker._acks[event['operation_id']] = dict(type='ack', operation_id=event['operation_id'], config_version=event['config_version'], context_id=event['context_id'])
    worker._enqueue = enqueue
    worker.release_context()
    assert worker.submit_monitor(1) is False
    assert worker.resume_commands() is False


def test_initialize_context_requires_completed_release(tmp_path):
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused')
    worker._state = 'running'
    with pytest.raises(RuntimeError, match='release'):
        worker.initialize_context(config, 2)


def test_release_cancellation_waits_for_ack_before_revoking_globals(tmp_path):
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused', release_timeout_s=1)
    worker._state = 'running'
    worker.context_id = 'old'
    worker.global_state = type('Service', (), {'revoke': lambda self, owner: setattr(self, 'revoked', True)})()
    sent = threading.Event()
    cancel = threading.Event()
    event = {}
    def enqueue(message):
        if message.get('type') == 'reinit_release':
            event.update(message)
            sent.set()
    worker._enqueue = enqueue
    errors = []
    def release():
        try: worker.release_context(cancel)
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=release)
    thread.start()
    assert sent.wait(1)
    cancel.set()
    time.sleep(.08)
    assert thread.is_alive() and not getattr(worker.global_state, 'revoked', False)
    with worker._condition:
        worker._acks[event['operation_id']] = dict(type='ack', **{k:event[k] for k in ('operation_id','context_id','config_version')})
        worker._condition.notify_all()
    thread.join(1)
    assert not thread.is_alive() and not errors and worker.global_state.revoked


def test_candidate_rejection_is_distinct_from_protocol_error(tmp_path):
    from robot_control.process import CandidateRejected
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused')
    worker._state = 'running'
    def enqueue(event):
        if event.get('type') == 'reinit_validate':
            worker._acks[event['operation_id']] = dict(type='error', operation_id=event['operation_id'], error_type='candidate_rejected', error='bad', context_id=event['context_id'], config_version=event['config_version'])
    worker._enqueue = enqueue
    with pytest.raises(CandidateRejected, match='bad'):
        worker.validate_reinitialize(config, 2)


def test_stop_interrupts_sent_release_without_revoking_globals(tmp_path):
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused', release_timeout_s=20)
    worker._state = 'running'
    worker.context_id = 'old'
    worker.global_state = type('Service', (), {'revoke': lambda self, owner: setattr(self, 'revoked', True)})()
    sent, stop = threading.Event(), threading.Event()
    worker._enqueue = lambda event: sent.set() if event.get('type') == 'reinit_release' else None
    errors = []
    def release():
        try: worker.release_context(None, stop=stop)
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=release)
    thread.start()
    assert sent.wait(1)
    stop.set()
    thread.join(1)
    assert not thread.is_alive() and errors
    assert worker._context_enabled is False
    assert not getattr(worker.global_state, 'revoked', False)
    worker.revoke_context()
    assert worker.global_state.revoked


def test_stale_candidate_rejection_is_protocol_error(tmp_path):
    from robot_control.process import CandidateRejected
    config = {'id':'a', 'tui_commands':{'reload': {'handler':'reload', 'args':[], 'effect':'reinitialize_policy'}}}
    worker = PolicyProcess(tmp_path, config, host_socket='/tmp/unused')
    worker._state = 'running'
    def enqueue(event):
        if event.get('type') == 'reinit_validate':
            worker._acks[event['operation_id']] = dict(type='error', operation_id=event['operation_id'], error_type='candidate_rejected', error='bad', context_id='stale', config_version=event['config_version'])
    worker._enqueue = enqueue
    with pytest.raises(RuntimeError, match='stale') as result:
        worker.validate_reinitialize(config, 2)
    assert not isinstance(result.value, CandidateRejected)
