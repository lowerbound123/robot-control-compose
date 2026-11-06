"""Backend switching changes the independent connector, not the policy process."""
import json

import pytest

from test_example_controller import example_rc, command


def connector_profile(rc, name='second', position=.4):
    folder = rc.root / 'robots'
    folder.mkdir(exist_ok=True)
    device = folder / (name + '.device.json')
    device.write_text(json.dumps({'initial_qpos': [position] * 14, 'image_size': [12, 16]}))
    (folder / (name + '.yaml')).write_text(json.dumps({'connector': 'body', 'source': 'mock', 'config': str(device)}))
    return name, device


def test_switch_replaces_connector_process_and_preserves_loaded_policy(example_rc):
    rc = example_rc
    old_connector = rc.robot.raw
    old_pid = old_connector.process.pid
    worker = rc._worker
    before = command(rc, 'policy example example')
    profile, _ = connector_profile(rc)
    command(rc, 'backend switch ' + profile)
    after = command(rc, 'policy example example')
    assert old_connector.closed and old_connector.process.poll() == 0
    assert rc.robot.raw.process.pid != old_pid
    assert rc._worker is worker and after['module_id'] == before['module_id']
    assert after['context_id'] != before['context_id']
    assert rc.robot.read_state()['qpos'] == pytest.approx([.4] * 14)
    assert rc.status()['state'] == 'paused'


def test_invalid_device_preflight_preserves_connection_and_recording(example_rc):
    rc = example_rc
    command(rc, 'record start')
    session = rc.status()['recording']['session_id']
    old_connector = rc.robot.raw
    profile, device = connector_profile(rc)
    device.write_text('feedback_timeout_s: -1\n')
    command(rc, 'backend switch ' + profile, status='failed')
    assert rc.robot.raw is old_connector and old_connector.process.poll() is None
    assert rc.status()['recording']['session_id'] == session
    assert rc.status()['backend']['connected']


def test_new_connector_receipts_cannot_consume_previous_publications(example_rc):
    rc = example_rc
    profile, _ = connector_profile(rc)
    old_generation = rc.robot.generation
    command(rc, 'backend switch ' + profile)
    assert rc.robot.generation > old_generation
    assert all(pending['generation'] == rc.buffer.generation for pending in rc._publications.values())
    assert rc.buffer.consumed == 0
    assert rc.robot.raw.runtime is rc.runtime


def test_switch_drains_old_receipts_before_recording_boundary_and_stops_holds(example_rc, monkeypatch):
    import threading
    import time
    from test_controller import until
    rc = example_rc
    command(rc, 'record start')
    session = rc.status()['recording']['session_id']
    connector = rc.robot.raw
    release = threading.Event()
    poll = connector.poll_results
    monkeypatch.setattr(connector, 'poll_results', lambda: poll() if release.is_set() else [])
    until(lambda: bool(rc._publications))
    profile, _ = connector_profile(rc)
    operation = rc.submit('backend switch ' + profile)
    try:
        until(lambda: rc.status()['backend']['phase'] == 'draining')
        submitted = connector.client.serial
        time.sleep(.1)
        assert connector.client.serial == submitted
        assert rc.status()['recording']['session_id'] == session
        assert connector.process.poll() is None
    finally:
        release.set()
    receipt = rc.wait_operation(operation, 10)
    assert receipt['status'] == 'succeeded', receipt
    assert rc.status()['recording']['session_id'] != session
    assert connector.closed and not connector._sent
