import json

import pytest

from test_controller import setup_controller, until


def test_rc_config_draft_does_not_change_runtime_or_disk(setup_controller):
    rc, _, _ = setup_controller
    active = rc.config['frequency_hz']
    original = rc.config_path.read_text()
    rc.start()
    until(lambda: rc.status()['state'] == 'paused' and rc.status()['policy'] == 'first')
    identity = rc.submit('config rc set frequency_hz 17')
    until(lambda: rc.get_operation(identity)['status'] == 'succeeded')
    snapshot = rc.query('config rc show')
    assert rc.config['frequency_hz'] == active
    assert rc.config_path.read_text() == original
    assert snapshot['draft']['frequency_hz'] == 17
    assert snapshot['changes']['frequency_hz'] == {'active': active, 'draft': 17}
    saved = rc.submit('config rc save')
    until(lambda: rc.get_operation(saved)['status'] == 'succeeded')
    from robot_control.yaml_config import load_yaml_config
    assert load_yaml_config(rc.config_path)['frequency_hz'] == 17
    assert rc.config['frequency_hz'] == active


def test_old_control_tokens_never_execute_and_resume_does_not_return(setup_controller):
    rc, _, _ = setup_controller
    rc.start()
    until(lambda: rc.status()['state'] == 'paused' and rc.status()['policy'] == 'first')
    for line in ('take', 'return', 'r', 'quit', 'switch a'):
        with pytest.raises(ValueError):
            rc.submit(line)
    taken = rc.submit_control('take')
    until(lambda: rc.get_operation(taken)['status'] == 'succeeded')
    assert rc.status()['control_owner'] == 'mcp'
    resumed = rc.submit('resume')
    until(lambda: rc.get_operation(resumed)['status'] == 'failed')
    assert rc.status()['control_owner'] == 'mcp'
    assert 'mcp return' in rc.get_operation(resumed)['error']


def test_config_save_requires_a_running_instance(setup_controller):
    rc, _, root = setup_controller
    rc.start()
    until(lambda: rc.status()['policy'] == 'first' and rc.status()['state'] == 'paused')
    path = root / 'policy/second/policy.yaml'
    before = path.read_bytes()
    reply = rc.wait_operation(rc.submit('policy second config save'), 3)
    assert reply['status'] == 'failed', reply
    assert path.read_bytes() == before


def test_internal_operation_labels_cannot_be_intercepted_by_policy_commands(setup_controller):
    rc, _, _ = setup_controller
    rc.start()
    until(lambda: rc.status()['policy'] == 'first' and rc.status()['state'] == 'paused')
    rc._worker.command_specs = {'rc_config_set': {'handler': 'anything', 'args': [], 'timeout_s': 5}}
    reply = rc.wait_operation(rc.submit('config rc set frequency_hz 19'), 3)
    assert reply['status'] == 'succeeded', reply
    assert rc.query('config rc show')['draft']['frequency_hz'] == 19
