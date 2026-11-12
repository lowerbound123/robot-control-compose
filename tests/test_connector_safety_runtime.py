"""Limits follow connection commits while the existing RC motion loop stays in charge."""
import copy
import json

import numpy as np
import pytest

from robot_control.connector import ConnectorRobot
from test_controller import ready, setup_controller
from test_example_controller import command, example_rc, example_mcp
from test_mcp_management import query


def device_profile(rc, name, safety=None, position=.2):
    folder = rc.root / rc.config.get('robot_profiles_directory', 'robots')
    folder.mkdir(parents=True, exist_ok=True)
    device = folder / (name + '-device.json')
    config = {'initial_qpos': [position] * 14, 'initial_epos': [position] * 14, 'image_size': [12, 16]}
    if safety is not None:
        config['safety'] = safety
    device.write_text(json.dumps(config))
    (folder / (name + '.yaml')).write_text(json.dumps({'connector': 'body', 'source': 'mock', 'config': str(device)}))
    return device


def test_initial_connection_installs_device_limits_before_motion(setup_controller):
    rc, robot, _ = setup_controller
    robot.config['safety'] = {'enabled': True, 'max_delta_per_step': [.01] * 14}
    ready(rc)
    assert rc.limiter.enabled is True
    np.testing.assert_allclose(rc.limiter.limits, .005)
    assert rc.connector_properties()['safety']['max_delta_per_step'] == [.01] * 14
    assert rc.config['safety']['enabled'] is False


def test_switch_limits_change_real_sends_and_missing_device_settings_restore_rc_fallback(example_rc):
    rc = example_rc
    fallback = copy.deepcopy(rc.config['safety'])
    device_profile(rc, 'limited', {'enabled': True, 'reference_frequency_hz': 50,
                                   'max_delta_per_step': [.006] * 14})
    command(rc, 'connector switch limited')
    limited = rc.limiter
    np.testing.assert_allclose(limited.previous, .2)
    np.testing.assert_allclose(limited.limits, .01)
    command(rc, 'reset')
    moving = [row for row in rc.robot.commands if row[0] < .199]
    assert moving
    np.testing.assert_allclose(moving[0], .19, atol=1e-7)
    assert rc.config['safety'] == fallback
    device_profile(rc, 'fallback', position=-.4)
    command(rc, 'connector switch fallback')
    assert rc.limiter is not limited
    assert rc.connector_properties()['safety'] == fallback
    np.testing.assert_allclose(rc.limiter.previous, -.4)
    np.testing.assert_allclose(rc.limiter.limits, 1000000. * 50 / 30)


def test_invalid_safety_preflight_preserves_old_connection_limiter_and_recording(example_rc):
    rc = example_rc
    command(rc, 'record start')
    session = rc.status()['recording']['session_id']
    old, limiter, generation = rc.robot.raw, rc.limiter, rc.robot.generation
    device_profile(rc, 'bad', {'epos': {'rotation_rad_s': 0}})
    command(rc, 'connector switch bad', status='failed')
    assert rc.robot.raw is old and rc.limiter is limiter
    assert rc.robot.generation == generation and rc.status()['connector']['connected']
    assert rc.status()['recording']['session_id'] == session


def test_connection_retry_reads_new_device_limits(example_rc, monkeypatch):
    rc = example_rc
    original, old_limiter = ConnectorRobot.connect, rc.limiter
    device = device_profile(rc, 'retry_limits', {'enabled': True, 'max_delta_per_step': [.01] * 14})
    def failed_connect(*args, **kwargs):
        raise RuntimeError('test feedback unavailable')
    monkeypatch.setattr(ConnectorRobot, 'connect', failed_connect)
    command(rc, 'connector switch retry_limits', status='failed')
    assert rc.limiter is old_limiter
    assert not rc.status()['connector']['connected']
    config = json.loads(device.read_text())
    config['safety']['max_delta_per_step'] = [.03] * 14
    device.write_text(json.dumps(config))
    monkeypatch.setattr(ConnectorRobot, 'connect', original)
    command(rc, 'connector retry')
    assert rc.connector_properties()['safety']['max_delta_per_step'] == [.03] * 14
    np.testing.assert_allclose(rc.limiter.limits, .05)
    np.testing.assert_allclose(rc.limiter.previous, .2)


def test_effective_limits_in_mcp_and_recording_follow_connection_snapshot(example_rc, example_mcp):
    rc = example_rc
    device = device_profile(rc, 'reported_limits', {'max_delta_per_step': [.02] * 14,
                                                  'epos': {'translation_m_s': .12}})
    command(rc, 'connector switch reported_limits')
    effective = query(example_mcp, 'props')['safety']
    assert effective['max_delta_per_step'] == [.02] * 14
    assert effective['epos']['translation_m_s'] == .12
    assert effective['epos']['rotation_rad_s'] == 1000000.
    assert rc._record_metadata()['safety'] == effective
    device.write_text(json.dumps({'safety': {'max_delta_per_step': [.04] * 14}}))
    assert query(example_mcp, 'props')['safety'] == effective
    command(rc, 'connector switch reported_limits')
    assert query(example_mcp, 'props')['safety']['max_delta_per_step'] == [.04] * 14
