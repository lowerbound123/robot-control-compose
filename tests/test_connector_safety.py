"""Device limits are frozen with a connection; RC owns the slew calculation."""
import copy
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from config_helpers import read_config
from robot_control.config import _validate_rc
from robot_control.connector import ConnectorRobot
from robot_control.safety import ModeLimiter

ROOT = Path(__file__).resolve().parents[1]


def test_missing_rc_safety_has_large_finite_defaults_in_both_modes():
    config = read_config(ROOT / 'configs/rc.mock.yaml.example')
    config.pop('safety')
    _validate_rc(config)
    safety = config['safety']
    assert safety == {'enabled': True, 'reference_frequency_hz': 50,
                      'max_delta_per_step': [1000000.] * 14,
                      'epos': {'translation_m_s': 1000000., 'rotation_rad_s': 1000000.,
                               'gripper_units_s': 1000000.}}
    limiter = ModeLimiter(safety, 100)
    limiter.reset(np.zeros(14))
    state = {'qpos': [0.] * 14, 'eef': [0.] * 14}
    for mode in ('qpos', 'epos'):
        sent, complete = limiter.apply([1.] * 14, mode, state)
        assert complete
        np.testing.assert_array_equal(sent, np.ones(14))


def test_device_override_inherits_missing_fields_without_mutating_rc():
    from robot_control.safety_config import safety_settings
    fallback = {'enabled': False, 'reference_frequency_hz': 40,
                'max_delta_per_step': [.2] * 14,
                'epos': {'translation_m_s': .3, 'rotation_rad_s': .4, 'gripper_units_s': .5}}
    device = {'enabled': True, 'max_delta_per_step': [.01] * 14,
              'epos': {'rotation_rad_s': .8}}
    before = copy.deepcopy((fallback, device))
    effective = safety_settings(device, fallback=fallback)
    assert effective == {'enabled': True, 'reference_frequency_hz': 40,
                         'max_delta_per_step': [.01] * 14,
                         'epos': {'translation_m_s': .3, 'rotation_rad_s': .8, 'gripper_units_s': .5}}
    limiter = ModeLimiter(effective, 100)
    limiter.reset(np.zeros(14))
    sent, complete = limiter.apply([1.] * 14, 'qpos', {'qpos': [0.] * 14})
    assert not complete
    np.testing.assert_allclose(sent, .004, atol=1e-9)
    effective['max_delta_per_step'][0] = 99
    effective['epos']['translation_m_s'] = 99
    assert (fallback, device) == before


def proxy(tmp_path, safety):
    device = tmp_path / 'device.yaml'
    device.write_text(json.dumps({'image_size': [12, 16], 'safety': safety}))
    config = read_config(ROOT / 'configs/rc.mock.yaml.example')
    config['safety'] = {'enabled': False, 'max_delta_per_step': [.2] * 14}
    config['connectors']['body']['config'] = str(device)
    return ConnectorRobot(config, ROOT), device


@pytest.mark.parametrize('safety', [None, [], {'enabled': 'false'}, {'max_delta_per_step': [0] * 14},
                                   {'max_delta_per_step': [1] * 13}, {'max_delta_per_step': [1e40] * 14},
                                   {'reference_frequency_hz': 0}, {'epos': None},
                                   {'epos': {'translation_m_s': -1}}, {'epos': {'rotation_rad_s': True}},
                                   {'epos': {'unknown': 1}}, {'typo': 1}])
def test_invalid_device_safety_fails_preflight_before_starting_device(tmp_path, safety):
    robot, _ = proxy(tmp_path, safety)
    with pytest.raises(ValueError, match='safety'):
        robot.prepare()
    assert robot.process is None and robot.client is None and not robot._prepared


def test_prepared_safety_is_frozen_until_new_connection(tmp_path):
    robot, device = proxy(tmp_path, {'enabled': True, 'max_delta_per_step': [.01] * 14})
    robot.prepare()
    device.write_text(json.dumps({'safety': {'max_delta_per_step': [.03] * 14}}))
    robot.rc_config['safety']['enabled'] = True
    assert robot.safety['enabled'] is True
    assert robot.safety['max_delta_per_step'] == [.01] * 14
    assert robot.config['safety']['max_delta_per_step'] == [.01] * 14
    next_robot = ConnectorRobot(robot.rc_config, ROOT)
    next_robot.prepare()
    assert next_robot.safety['max_delta_per_step'] == [.03] * 14


def test_setup_validates_device_safety_with_the_same_rules():
    spec = importlib.util.spec_from_file_location('safety_setup_validation', ROOT / 'scripts/config_validation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = 'connector/custom/devices/robot.yaml'
    with pytest.raises(ValueError, match=path):
        module.validate_documents({path: {'safety': {'epos': {'rotation_rad_s': 0}}}})
    module.validate_documents({path: {'safety': {'max_delta_per_step': [-1] * 14}}})


def test_setup_includes_device_safety_in_validation_without_overwriting_private_values(tmp_path):
    from test_setup_config import helpers
    folder = tmp_path / 'connector/mock/devices'
    folder.mkdir(parents=True)
    template = folder / 'default.yaml.example'
    template.write_text('safety:\n  enabled: true\n')
    path = template.with_suffix('')
    changes, documents = helpers().configuration_updates(tmp_path)
    assert documents['connector/mock/devices/default.yaml']['safety']['enabled'] is True
    assert any(target == path for target, _ in changes)
    path.write_text('safety:\n  enabled: false\n')
    changes, documents = helpers().configuration_updates(tmp_path)
    assert documents['connector/mock/devices/default.yaml']['safety']['enabled'] is False
    assert not any(target == path for target, _ in changes)


def test_rc_templates_use_large_fallback_and_device_templates_keep_prior_limits():
    for path in [ROOT / 'rc.yaml.example', *sorted((ROOT / 'configs').glob('rc.*.yaml.example'))]:
        config = read_config(path)
        assert config['safety']['enabled'] is True
        assert config['safety']['max_delta_per_step'] == [1000000.] * 14
        assert set(config['safety']['epos'].values()) == {1000000.}
    for path in ('mock/devices/default.yaml.example', 'acone/devices/robot-main.yaml.example',
                 'acone/devices/ros-test.yaml.example'):
        safety = read_config(ROOT / 'connector' / path)['safety']
        assert safety['max_delta_per_step'][0] == .006072
        assert safety['max_delta_per_step'][6] == .05
        assert safety['epos']['translation_m_s'] == .15
