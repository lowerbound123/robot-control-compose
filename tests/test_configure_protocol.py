"""Configure transactions must not leak candidates or unconfirmed active state."""
from contextlib import contextmanager
import copy
import socket
import threading

import pytest

from rc_policy_sdk.worker import run_session
from rc_policy_sdk.wire import receive_message, send_message


POLICY = '''
import threading
saved = None
def initialize(config):
    return {"config": config, "params": config["params"], "resets": 0}
def policy(context, state, action_chunk, images, config):
    assert context["config"] is config
    assert context["params"] is config["params"]
    return [[config["params"]["amount"]] * 14]
def reset_context(context):
    context["resets"] += 1
    if context["config"]["params"]["amount"] == 99: raise ValueError("reset failed")
def release(context): pass
def monitor(context):
    assert context["params"] is context["config"]["params"]
    return {"amount": context["params"]["amount"], "resets": context["resets"]}
def configure(context, config, key, value, callback):
    global saved
    if key == "expired":
        saved("reset")
    elif key == "thread":
        errors = []
        def run():
            try: callback("reset")
            except RuntimeError: errors.append(1)
        thread = threading.Thread(target=run)
        thread.start(); thread.join()
        assert errors == [1]
    elif key == "metadata":
        config["id"] = "changed"
    elif key == "mode":
        config["execution"]["control_mode"] = value
    else:
        config["params"]["amount"] = int(value)
        if key == "reset": callback("reset"); callback("reset")
        if key == "reinitialize": callback("reinitialize")
        if key == "conflict": callback("reset"); callback("reinitialize")
        if key == "reject": raise ValueError("bad value")
    saved = callback
'''


@contextmanager
def worker(tmp_path, source=POLICY, *, backend_switch=False):
    (tmp_path / 'main.py').write_text(source)
    left, right = socket.socketpair()
    left.settimeout(3)
    thread = threading.Thread(target=run_session, args=(right, tmp_path, 'configure_test'))
    thread.start()
    config = {'id': 'example-0', 'params': {'amount': 1}, 'execution': {'control_mode': 'qpos'}, 'hot_reloadable': []}
    def send(kind, **extras):
        send_message(left, dict(type=kind, operation_id=kind, config_version=1, context_id='old', **extras))
    try:
        send('initialize', config=config, configure=True, backend_switch_v1=backend_switch)
        ready = receive_message(left)
        assert ready['type'] == 'ready'
        receive_message(left)
        yield send, lambda: receive_message(left), ready, config
    finally:
        try: send('shutdown')
        except OSError: pass
        thread.join(3)
        left.close()
        assert not thread.is_alive()


def test_configure_is_independent_of_custom_effects_and_defers_mutation(tmp_path):
    with worker(tmp_path) as (send, recv, ready, config):
        assert ready.get('configure') is True
        send('configure_prepare', key='reset', value='7')
        result = recv()
        assert result['type'] == 'ack'
        assert result['action'] == 'reset'
        assert result['config']['params']['amount'] == 7
        send('configure_apply', config=result['config'], action='reset', new_config_version=2)
        assert recv()['config_version'] == 2
        # Arbitrary resume cannot cross the uncommitted configure fence.
        send('command_resume')
        send('infer', state={}, images={}, action_chunk=[], request_id=1, generation=1)
        assert recv()['type'] == 'error'
        # send helper supplies old identity; commit has its own next-version field.
        send('configure_commit', new_config_version=2)
        assert recv()['type'] == 'ack'
        send('command_resume')
        # Version 1 must remain rejected after successful commit.
        send('infer', state={}, images={}, action_chunk=[], request_id=2, generation=1)
        assert recv()['type'] == 'error'


@pytest.mark.parametrize('key,value', [('metadata', 'x'), ('mode', 'epos'), ('conflict', '2'), ('reject', '2')])
def test_rejected_candidate_never_changes_live_config(tmp_path, key, value):
    with worker(tmp_path) as (send, recv, ready, config):
        send('configure_prepare', key=key, value=value)
        rejected = recv()
        assert rejected.get('error_type') == 'candidate_rejected'
        send('configure_abort')
        assert recv()['type'] == 'ack'
        send('command_resume')
        send('monitor', monitor_id=1, monitor_epoch=1)
        result = recv()
        assert result['type'] == 'monitor_result', result.get('error')
        assert result['result']['amount'] == 1


def test_callback_expires_and_is_thread_bound(tmp_path):
    with worker(tmp_path) as (send, recv, ready, config):
        send('configure_prepare', key='thread', value='2')
        assert recv()['type'] == 'ack'
        send('configure_abort')
        assert recv()['type'] == 'ack'
        send('configure_prepare', key='expired', value='2')
        assert recv().get('error_type') == 'candidate_rejected'


def test_configure_reinitialize_without_manifest_command(tmp_path):
    with worker(tmp_path) as (send, recv, ready, config):
        send('configure_prepare', key='reinitialize', value='5')
        prepared = recv()
        assert prepared.get('action') == 'reinitialize'
        send('reinit_validate', config=prepared['config'], new_config_version=2)
        assert recv()['type'] == 'ack'
        send('reinit_release')
        assert recv()['type'] == 'ack'


def test_legacy_policy_advertises_configure_unsupported(tmp_path):
    with worker(tmp_path, POLICY.split('def configure(')[0]) as (send, recv, ready, config):
        assert ready.get('configure') is False
        send('configure_prepare', key='amount', value='2')
        assert recv().get('error_type') == 'candidate_rejected'


@contextmanager
def process(tmp_path, source=POLICY):
    from host_helpers import LocalPolicyProcess
    folder = tmp_path / 'example'
    folder.mkdir()
    (folder / 'main.py').write_text(source)
    proc = LocalPolicyProcess(folder, {'id': 'example-0', 'params': {'amount': 1}, 'hot_reloadable': []},
                              init_timeout_s=5, inference_timeout_s=2, release_timeout_s=2)
    try:
        proc.start()
        yield proc
    finally:
        proc.close()


@pytest.mark.parametrize(('key', 'resets'), [('amount', 0), ('reset', 1)])
def test_process_publish_is_separate_from_worker_apply_and_ack(tmp_path, key, resets):
    from test_host_transport import wait_for
    with process(tmp_path) as proc:
        assert proc.supports_configure is True
        original, params, pid = proc.config, proc.config['params'], proc.pid
        prepared = proc.prepare_config(key, '7')
        proc.apply_config(prepared['config'], 2, prepared['action'])
        assert proc.config['params']['amount'] == 1
        assert proc.config_version == 1
        assert proc.resume_commands() is False
        proc.commit_config(prepared['config'], 2)
        assert proc.config['params']['amount'] == 1
        assert proc.resume_commands() is False
        proc.publish_config(prepared['config'], 2)
        assert proc.config is original and proc.config['params'] is params
        assert proc.config_version == 2 and params['amount'] == 7
        assert proc.resume_commands() is True
        assert proc.submit_monitor(1)
        reply = wait_for(proc.poll_monitor)
        assert reply['result'] == {'amount': 7, 'resets': resets}
        assert proc.pid == pid


def test_process_rejected_prepare_can_abort_but_failed_apply_stays_fenced(tmp_path):
    from robot_control.process import CandidateRejected
    with process(tmp_path) as proc:
        with pytest.raises(CandidateRejected):
            proc.prepare_config('reject', '4')
        proc.abort_config()
        assert proc.resume_commands() is True
        prepared = proc.prepare_config('reset', '99')
        with pytest.raises(RuntimeError, match='reset failed'):
            proc.apply_config(prepared['config'], 2, prepared['action'])
        assert proc.config['params']['amount'] == 1 and proc.config_version == 1
        assert proc.resume_commands() is False
        with pytest.raises(RuntimeError): proc.abort_config()


def test_process_configure_reinitialize_keeps_execution_pid(tmp_path):
    from test_host_transport import wait_for
    with process(tmp_path) as proc:
        pid, old_context = proc.pid, proc.context_id
        prepared = proc.prepare_config('reinitialize', '8')
        proc.validate_reinitialize(prepared['config'], 2)
        proc.release_context()
        flags = proc.initialize_context(prepared['config'], 2)
        proc.commit_reinitialize(prepared['config'], 2, flags)
        assert proc.resume_commands() is True
        assert proc.context_id != old_context and proc.pid == pid
        assert proc.submit_monitor(1)
        assert wait_for(proc.poll_monitor)['result'] == {'amount': 8, 'resets': 0}


def test_process_stale_apply_ack_never_resumes(tmp_path):
    with process(tmp_path) as proc:
        prepared = proc.prepare_config('amount', '3')
        original_receive = proc._receive
        def stale_ack(packet):
            packet = copy.deepcopy(packet)
            if packet['event'].get('type') == 'ack' and packet['event'].get('config_version') == 2:
                packet['event']['context_id'] = 'expired-context'
            original_receive(packet)
        proc._receive = stale_ack
        with pytest.raises(RuntimeError, match='stale configure'):
            proc.apply_config(prepared['config'], 2, prepared['action'])
        assert proc.runnable is False
        assert proc.config['params']['amount'] == 1
        assert proc.config_version == 1
        assert proc.resume_commands() is False
        with pytest.raises(RuntimeError): proc.abort_config()
        with pytest.raises(RuntimeError, match='cold'):
            proc.validate_reinitialize(prepared['config'], 2)


@pytest.mark.parametrize('stage', ['applied', 'committed'])
def test_guard_failure_marks_retained_process_unrunnable_without_publishing(tmp_path, stage):
    with process(tmp_path) as proc:
        original, params, pid = proc.config, proc.config['params'], proc.pid
        prepared = proc.prepare_config('amount', '3')
        assert proc.runnable
        proc.apply_config(prepared['config'], 2, prepared['action'])
        assert proc.runnable
        if stage == 'committed':
            proc.commit_config(prepared['config'], 2)
            assert proc.runnable
        proc.mark_config_failed()
        proc.mark_config_failed()
        assert not proc.runnable
        assert proc.config is original and proc.config['params'] is params
        assert params['amount'] == 1 and proc.config_version == 1
        assert proc.pid == pid and proc.has_readers
        assert proc.resume_commands() is False
        assert proc.observe(dict(request_id=1, generation=1, config_version=1, state={}, images={})) is False
        assert proc.submit_monitor(1) is False
        with pytest.raises(RuntimeError):
            proc.submit(dict(request_id=1, generation=1, config_version=1, state={}, images={}, action_chunk=[]))
        with pytest.raises(RuntimeError): proc.reset_context()
        with pytest.raises(RuntimeError): proc.publish_config(prepared['config'], 2)
        with pytest.raises(RuntimeError, match='cold'):
            proc.validate_reinitialize(prepared['config'], 2)
        with pytest.raises(RuntimeError, match='cold'):
            proc.commit_reinitialize(prepared['config'], 2, {'observe': False, 'monitor': True})


def test_prepare_waits_for_active_monitor_and_does_not_run_reset_early(tmp_path):
    import time
    from test_host_transport import wait_for
    source = POLICY.replace('def monitor(context):', '''def monitor(context):
    import time
    from pathlib import Path
    root = Path(ROOT)
    (root / 'started').touch()
    while not (root / 'go').exists(): time.sleep(.005)
'''.replace('ROOT', repr(str(tmp_path))) + '    assert True\n')
    with process(tmp_path, source) as proc:
        assert proc.submit_monitor(1)
        wait_for(lambda: (tmp_path / 'started').exists())
        result, errors = [], []
        def prepare():
            try: result.append(proc.prepare_config('reset', '6'))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=prepare)
        thread.start()
        try:
            time.sleep(.05)
            assert not result and not errors
        finally:
            (tmp_path / 'go').touch()
            thread.join(3)
        assert not errors
        assert result[0]['config']['params']['amount'] == 6
        assert proc.config['params']['amount'] == 1
        proc.abort_config()
        assert proc.resume_commands()
        wait_for(proc.poll_monitor)
        assert proc.submit_monitor(2)
        assert wait_for(proc.poll_monitor)['result'] == {'amount': 1, 'resets': 0}


def test_cancelled_running_prepare_stays_fenced_even_after_late_ack(tmp_path):
    from test_host_transport import wait_for
    source = POLICY.replace('    global saved\n', '''    global saved
    import time
    from pathlib import Path
    root = Path(ROOT)
    (root / 'started').touch()
    while not (root / 'go').exists(): time.sleep(.005)
'''.replace('ROOT', repr(str(tmp_path))))
    with process(tmp_path, source) as proc:
        cancel = threading.Event()
        errors = []
        def prepare():
            try: proc.prepare_config('amount', '9', cancel)
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=prepare)
        thread.start()
        try:
            wait_for(lambda: (tmp_path / 'started').exists())
            cancel.set()
            thread.join(3)
            assert len(errors) == 1 and 'cancelled' in str(errors[0])
        finally:
            (tmp_path / 'go').touch()
            thread.join(3)
        assert not proc.resume_commands()
        with pytest.raises(RuntimeError): proc.abort_config()
        assert proc.config['params']['amount'] == 1


def test_invalid_execution_candidate_is_rejected_before_prepare_ack(tmp_path):
    source = POLICY.replace('config["execution"]["control_mode"] = value',
                            'config["execution"]["control_mode"] = value; callback("reinitialize")')
    with worker(tmp_path, source) as (send, recv, ready, config):
        send('configure_prepare', key='mode', value='invalid')
        assert recv().get('error_type') == 'candidate_rejected'


def test_legacy_lifecycle_cannot_bypass_prepared_configure_fence(tmp_path):
    with process(tmp_path) as proc:
        proc.prepare_config('amount', '3')
        with pytest.raises(RuntimeError, match='configure'):
            proc.reset_context()
        with pytest.raises(RuntimeError, match='configure'):
            proc.update_config({'params.amount': 2}, 2)
        proc.abort_config()
        assert proc.resume_commands()


def test_failed_apply_cannot_be_repaired_by_warm_reinitialize(tmp_path):
    with process(tmp_path) as proc:
        prepared = proc.prepare_config('reset', '99')
        with pytest.raises(RuntimeError):
            proc.apply_config(prepared['config'], 2, 'reset')
        with pytest.raises(RuntimeError, match='cold'):
            proc.validate_reinitialize(prepared['config'], 2)


def test_worker_legacy_reset_cannot_resume_uncommitted_callbacks(tmp_path):
    with worker(tmp_path) as (send, recv, ready, config):
        send('configure_prepare', key='amount', value='7')
        assert recv()['type'] == 'ack'
        send('reset_context')
        assert recv()['type'] == 'error'
        send('monitor', monitor_id=1, monitor_epoch=1)
        assert recv()['type'] == 'monitor_error'


def test_callback_rejects_unknown_lifecycle_action(tmp_path):
    source = POLICY.replace('callback("reset")', 'callback("restart")')
    with worker(tmp_path, source) as (send, recv, ready, config):
        send('configure_prepare', key='reset', value='7')
        assert recv().get('error_type') == 'candidate_rejected'


def test_apply_cannot_substitute_json_equal_python_boolean_for_prepared_integer(tmp_path):
    with process(tmp_path) as proc:
        prepared = proc.prepare_config('amount', '1')
        prepared['config']['params']['amount'] = True
        with pytest.raises(ValueError, match='prepared'):
            proc.apply_config(prepared['config'], 2, prepared['action'])
        proc.abort_config()
        assert proc.resume_commands()


def test_candidate_must_roundtrip_without_changing_python_value_types(tmp_path):
    source = POLICY.replace('config["params"]["amount"] = int(value)',
                            'config["params"]["amount"] = (int(value),)')
    with worker(tmp_path, source) as (send, recv, ready, config):
        send('configure_prepare', key='amount', value='7')
        assert recv().get('error_type') == 'candidate_rejected'


@pytest.mark.parametrize("source", [POLICY, POLICY.split("def configure(")[0]])
def test_backend_capability_enables_context_handoff_without_manifest_effect(tmp_path, source):
    with worker(tmp_path, source, backend_switch=True) as (send, recv, ready, config):
        assert ready["backend_switch_v1"] is True
        assert ready["configure"] is (source == POLICY)
        send("reinit_validate", config=config, new_config_version=2)
        assert recv()["type"] == "ack"
        send("reinit_release")
        assert recv()["type"] == "ack"


def test_configure_capability_does_not_claim_unnegotiated_backend_switch(tmp_path):
    with worker(tmp_path) as (_, _, ready, _):
        assert ready["configure"] is True
        assert ready["backend_switch_v1"] is False


@pytest.mark.parametrize("value", ["unsupported", None, 1, True])
def test_configure_rejects_invalid_backend_adaptation_before_apply(tmp_path, value):
    source = POLICY.replace('config["execution"]["control_mode"] = value',
                            'config["execution"]["on_backend_switch"] = ' + repr(value))
    with worker(tmp_path, source, backend_switch=True) as (send, recv, _, _):
        send("configure_prepare", key="mode", value="ignored")
        error = recv()
        assert error.get("error_type") == "candidate_rejected"
        assert "on_backend_switch" in error["error"]
        send("configure_abort")
        assert recv()["type"] == "ack"


def test_process_backend_and_configure_capabilities_share_context_failure_gate(tmp_path):
    with process(tmp_path) as proc:
        assert proc.supports_configure and proc.supports_backend_switch
        assert proc.runnable
        proc.revoke_context()
        assert not proc.runnable
        request = dict(request_id=1, generation=1, config_version=proc.config_version,
                       state={}, action_chunk=[], images={})
        with pytest.raises(RuntimeError, match="not ready"):
            proc.submit(request)
        assert proc.observe(request) is False
        assert proc.submit_monitor(1) is False
        with pytest.raises(RuntimeError, match="not ready"):
            proc.reset_context()
