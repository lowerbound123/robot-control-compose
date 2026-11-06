"""Real example workers across connection replacement, without hardware."""
import json
import time

import pytest

from test_example_controller import example_rc, command, ROOT
from project_templates import copy_templates
from test_controller import setup_controller


def profile(rc, name='second', position=.2):
    folder = rc.root / rc.config.get('robot_profiles_directory', 'robots')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + '.yaml')).write_text(json.dumps(dict(backend='mock', initial_qpos=[position]*14,
                                                         initial_epos=[position]*14, image_size=[12, 16])))
    return name


def test_default_switch_reinitializes_without_replacing_worker(example_rc):
    rc = example_rc
    before = command(rc, 'example')
    worker = rc._worker
    old = rc.robot
    old_version = worker.config_version
    name = profile(rc)
    result = command(rc, 'backend switch ' + name)
    after = command(rc, 'example')
    assert rc._worker is worker and after['module_id'] == before['module_id']
    assert after['context_id'] != before['context_id']
    assert worker.config_version == old_version + 1
    assert rc.status()['backend']['profile'] == name
    assert rc.status()['state'] == 'paused'
    assert rc.robot.read_state()['qpos'] == pytest.approx([.2]*14)
    assert rc.status()['queue_remaining'] == 0


def test_hot_reset_choice_keeps_context_and_versions_on_switch(example_rc):
    rc = example_rc
    command(rc, 'set example.execution.on_backend_switch "reset"')
    command(rc, 'resume')
    from test_controller import until
    until(lambda: rc.status()['consumed'] > 1)
    command(rc, 'pause')
    before = command(rc, 'example')
    worker, version = rc._worker, rc._worker.config_version
    command(rc, 'backend switch ' + profile(rc))
    after = command(rc, 'example')
    assert rc._worker is worker and worker.config_version == version
    assert after['context_id'] == before['context_id']
    assert before['calls'] > 0 and after['calls'] == 0


def test_mixed_repeated_instances_follow_their_own_switch_configuration(example_rc):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'switch example_chain')
    command(rc, 'set example-0.execution.on_backend_switch "reset"')
    before = [command(rc, f'example-{i}-example') for i in range(2)]
    workers = rc._worker.member_workers()
    command(rc, 'backend switch ' + profile(rc))
    after = [command(rc, f'example-{i}-example') for i in range(2)]
    assert rc._worker.member_workers() == workers
    assert after[0]['context_id'] == before[0]['context_id']
    assert after[1]['context_id'] != before[1]['context_id']
    assert rc.status()['state'] == 'paused'


def test_bad_profile_preserves_connection_context_and_recording(example_rc):
    rc = example_rc
    command(rc, 'record on')
    session = rc.status()['recording']['session_id']
    before = command(rc, 'example')
    command(rc, 'backend switch absent', status='failed')
    assert command(rc, 'example')['context_id'] == before['context_id']
    assert rc.status()['recording']['session_id'] == session
    assert rc.robot.read_state()['qpos'][0] == pytest.approx(.01)


def test_recording_splits_sessions_on_connection_commit(example_rc):
    rc = example_rc
    command(rc, 'record on')
    old = rc.status()['recording']['session_id']
    command(rc, 'backend switch ' + profile(rc))
    status = rc.status()['recording']
    assert status['state'] == 'recording' and status['session_id'] != old


def test_partial_failures_continue_and_retry_only_failed_instance(example_rc, monkeypatch):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'switch example_chain')
    workers = rc._worker.member_workers()
    before = command(rc, 'example-1-example')
    monkeypatch.setattr(workers['example-0'], 'initialize_context', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('injected init failure')))
    command(rc, 'backend switch ' + profile(rc), status='failed')
    assert rc.status()['retry_reload_instances'] == ['example-0']
    assert workers['example-1'].config_version > 1
    healthy_context = workers['example-1'].context_id
    command(rc, 'resume', status='failed')
    command(rc, 'retry-reload example-0')
    assert rc._worker.member_workers()['example-1'] is workers['example-1']
    assert workers['example-1'].context_id == healthy_context
    assert rc.status()['retry_reload_instances'] == []
    assert rc.status()['state'] == 'paused'


def test_multiple_failed_instances_can_be_retried_in_any_order(example_rc, monkeypatch):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'switch example_chain')
    for worker in rc._worker.member_workers().values():
        monkeypatch.setattr(worker, 'initialize_context', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('injected failure')))
    command(rc, 'backend switch ' + profile(rc), status='failed')
    assert rc.status()['retry_reload_instances'] == ['example-0', 'example-1']
    command(rc, 'retry-reload example-1')
    healthy = rc._worker.member_workers()['example-1']
    command(rc, 'retry-reload example-0')
    assert rc._worker.member_workers()['example-1'] is healthy
    assert rc._worker.runnable and rc.status()['state'] == 'paused'


def test_old_close_failure_blocks_creation_until_explicit_retry(example_rc, monkeypatch):
    rc = example_rc
    old = rc.robot.raw
    original = old.close
    attempts = []
    def fail_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('old transport still alive')
        original()
    monkeypatch.setattr(old, 'close', fail_once)
    command(rc, 'backend switch ' + profile(rc), status='failed')
    assert not rc.status()['backend']['connected']
    assert rc.robot.raw is old and not old.closed
    assert rc.status()['backend']['retry_available']
    command(rc, 'backend retry')
    assert old.closed and rc.robot.raw is not old
    assert rc.status()['state'] == 'paused'


def test_record_off_during_switch_cancels_automatic_restart(example_rc, monkeypatch):
    import threading
    rc = example_rc
    command(rc, 'record on')
    entered, go = threading.Event(), threading.Event()
    old_close = rc.robot.raw.close
    def delayed_close():
        entered.set()
        assert go.wait(5)
        old_close()
    monkeypatch.setattr(rc.robot.raw, 'close', delayed_close)
    operation = rc.submit('backend switch ' + profile(rc))
    try:
        assert entered.wait(5)
        command(rc, 'record off')
    finally:
        go.set()
    assert rc.wait_operation(operation, 10)['status'] == 'succeeded'
    assert rc.status()['recording']['state'] != 'recording'


def test_backend_mcp_requires_take_and_preserves_owner(example_rc):
    rc = example_rc
    line = 'backend switch ' + profile(rc)
    with pytest.raises(PermissionError):
        rc.submit(line, origin='mcp')
    rc.wait_operation(rc.submit('take', origin='mcp'), 5)
    receipt = rc.wait_operation(rc.submit(line, origin='mcp'), 10)
    assert receipt['status'] == 'succeeded'
    assert rc.status()['control_owner'] == 'mcp' and rc.status()['state'] == 'paused'


def test_same_profile_reloads_file_and_never_reuses_old_hold(example_rc):
    rc = example_rc
    command(rc, 'backend switch ' + profile(rc, position=.3))
    retired = rc.robot.raw
    count = len(retired.commands)
    profile(rc, position=-.4)
    command(rc, 'backend switch second')
    time.sleep(.1)
    assert retired.closed and len(retired.commands) >= count
    assert all(values == pytest.approx([-.4]*14) for values in rc.robot.commands)


def test_switch_cancellation_after_new_binding_preserves_connection_and_requires_retry(example_rc, monkeypatch):
    rc = example_rc
    old = rc._worker.initialize_context
    def pause_after_initialize(*args, **kwargs):
        flags = old(*args, **kwargs)
        command(rc, 'pause')
        return flags
    monkeypatch.setattr(rc._worker, 'initialize_context', pause_after_initialize)
    command(rc, 'backend switch ' + profile(rc), status='cancelled')
    assert rc.status()['backend']['profile'] == 'second'
    assert rc.status()['retry_reload_instances'] == ['example']
    command(rc, 'retry-reload example')
    assert rc.status()['state'] == 'paused'


def test_connection_failure_keeps_model_and_resumes_recording_only_after_retry(example_rc, monkeypatch):
    import robot_control.backend_control as control
    from robot_control.robots.mock import MockRobot
    rc = example_rc
    command(rc, 'record on')
    old_session = rc.status()['recording']['session_id']
    old_context = command(rc, 'example')['context_id']
    make = control.make_robot
    class Broken(MockRobot):
        def connect(self, cancel_event=None):
            raise RuntimeError('new feedback unavailable')
    monkeypatch.setattr(control, 'make_robot', lambda config: Broken(config['robot']))
    command(rc, 'backend switch ' + profile(rc), status='failed')
    assert not rc.status()['backend']['connected']
    assert rc.status()['recording']['state'] != 'recording'
    assert rc._worker.runnable
    monkeypatch.setattr(control, 'make_robot', make)
    command(rc, 'backend retry')
    assert rc.status()['recording']['state'] == 'recording'
    assert rc.status()['recording']['session_id'] != old_session
    assert command(rc, 'example')['context_id'] != old_context


def test_running_old_request_cannot_supply_actions_or_images_to_new_backend(example_rc):
    from test_controller import until
    rc = example_rc
    command(rc, 'set example.params.inference_delay_s 0.2')
    command(rc, 'resume')
    until(lambda: rc.status()['in_flight'])
    old = rc.robot.raw
    command(rc, 'backend switch ' + profile(rc, position=.7))
    assert old.closed and rc.status()['queue_remaining'] == 0
    time.sleep(.1)
    assert rc.robot.commands and all(values == pytest.approx([.7]*14) for values in rc.robot.commands)
    assert rc._inflight is None and rc._observe_inflight is None


def test_queued_old_motion_is_cancelled_at_backend_boundary(example_rc):
    rc = example_rc
    name = profile(rc, position=.6)
    with rc._lock:
        switched = rc.submit('backend switch ' + name)
        motion = rc.submit('reset')
    assert rc.wait_operation(switched, 10)['status'] == 'succeeded'
    assert rc.wait_operation(motion, 5)['status'] == 'cancelled'
    assert rc.robot.read_state()['qpos'] == pytest.approx([.6]*14)


def test_return_interrupts_pending_backend_commit(example_rc, monkeypatch):
    import threading
    rc = example_rc
    command(rc, 'take')
    entered, go = threading.Event(), threading.Event()
    close = rc.robot.raw.close
    def slow_close():
        entered.set()
        assert go.wait(5)
        close()
    monkeypatch.setattr(rc.robot.raw, 'close', slow_close)
    operation = rc.submit('backend switch ' + profile(rc), origin='mcp')
    try:
        assert entered.wait(5)
        returned = rc.submit('return')
        rc.wait_operation(returned, 2)
    finally:
        go.set()
    receipt = rc.wait_operation(operation, 10)
    assert receipt['status'] == 'cancelled'
    assert rc.status()['backend']['profile'] != 'second'


def test_cancelled_chain_revokes_unprocessed_old_contexts(example_rc, monkeypatch):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'switch example_chain')
    workers = rc._worker.member_workers()
    initialize = workers['example-0'].initialize_context
    def interrupted(*args, **kwargs):
        flags = initialize(*args, **kwargs)
        command(rc, 'pause')
        return flags
    monkeypatch.setattr(workers['example-0'], 'initialize_context', interrupted)
    command(rc, 'backend switch ' + profile(rc), status='cancelled')
    assert rc.status()['retry_reload_instances'] == ['example-0', 'example-1']
    assert not workers['example-1']._context_enabled
    with pytest.raises(RuntimeError, match='stale policy context'):
        rc._external.handle(workers['example-1'], dict(method='register', remote_id='old',
                            context_id=workers['example-1'].context_id))


def test_named_startup_and_empty_policy_switch_can_be_saved(tmp_path):
    from config_helpers import read_config
    from host_helpers import LocalPolicyProcess
    from robot_control.controller import RobotController
    from test_controller import until
    config = read_config(ROOT / 'configs/rc.mock.yaml.example')
    config.update(policy_directory='policy', outputs_directory='outputs', startup_policy=None,
                  robot_profiles_directory='robots', robot={'profile': 'first'})
    (tmp_path / 'robots').mkdir()
    for name, value in [('first', .2), ('second', .4)]:
        (tmp_path / 'robots' / (name+'.yaml')).write_text(json.dumps({'backend': 'mock', 'initial_qpos': [value]*14}))
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(config))
    rc = RobotController(path, process_factory=LocalPolicyProcess)
    rc.start()
    try:
        until(lambda: rc.status()['state'] == 'paused')
        assert rc.status()['backend']['profile'] == 'first'
        assert rc.robot.read_state()['qpos'][0] == .2
        command(rc, 'backend switch second')
        assert read_config(path)['robot'] == {'profile': 'first'}
        command(rc, 'save-config rc')
        assert read_config(path)['robot'] == {'profile': 'second'}
        assert rc.status()['policy'] is None and rc.robot.read_state()['qpos'][0] == pytest.approx(.4)
    finally:
        rc.submit('exit')
        assert rc.done.wait(10) and rc.exit_code == 0


def test_unconfirmed_external_reader_keeps_old_connection_and_allows_retry(example_rc, monkeypatch):
    from contextlib import contextmanager
    rc = example_rc
    before = command(rc, 'example')['context_id']
    old = rc.robot.raw
    original = rc._external.reader_barrier
    @contextmanager
    def unconfirmed(*args):
        raise TimeoutError('external reader has not released images')
        yield
    monkeypatch.setattr(rc._external, 'reader_barrier', unconfirmed)
    command(rc, 'backend switch ' + profile(rc), status='failed')
    assert rc.robot.raw is old and not old.closed
    assert command(rc, 'example')['context_id'] == before
    assert rc.status()['backend']['retry_available']
    monkeypatch.setattr(rc._external, 'reader_barrier', original)
    command(rc, 'backend retry')
    assert old.closed and rc.status()['backend']['profile'] == 'second'


def test_record_on_during_initial_connect_retains_existing_recording_semantics(setup_controller, monkeypatch):
    import threading
    rc, robot, _ = setup_controller
    entered, go = threading.Event(), threading.Event()
    connect = robot.connect
    def slow_connect(cancel_event=None):
        entered.set()
        assert go.wait(5)
        connect(cancel_event=cancel_event)
    monkeypatch.setattr(robot, 'connect', slow_connect)
    rc.start()
    try:
        assert entered.wait(3)
        result = command(rc, 'record on')
        assert result['state'] == 'recording'
        assert rc.status()['recording']['state'] == 'recording'
    finally:
        go.set()


def test_automatic_recording_session_has_its_own_global_state_snapshot(example_rc):
    from pathlib import Path
    from test_controller import until
    rc = example_rc
    command(rc, 'set global.backend_marker 42')
    command(rc, 'record on')
    until(lambda: rc._record_global_version == rc._global_service.version)
    command(rc, 'backend switch ' + profile(rc))
    path = Path(rc.status()['recording']['path'])
    until(lambda: rc.status()['recording']['samples_written'] >= 2)
    command(rc, 'record off')
    configs = [json.loads(file.read_text()) for file in (path/'configs').glob('*.json')]
    assert any(row.get('kind') == 'globals' and row['value'].get('backend_marker') == 42 for row in configs)
