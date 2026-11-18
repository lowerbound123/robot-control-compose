import json

import numpy as np
import pytest

from test_connector_safety import proxy
from test_connector_switch_integration import connector_profile
from test_controller import ready, setup_controller
from test_example_controller import command, example_mcp, example_rc
from test_mcp_management import query


def test_initial_connector_owns_reset_including_both_grippers(setup_controller):
    rc, robot, _ = setup_controller
    target = [i / 16 for i in range(-7, 7)]
    robot.config['reset_qpos'] = target
    ready(rc)
    command(rc, 'reset')
    np.testing.assert_array_equal(robot.commands[-1], target)
    assert rc.connector_properties()['reset_qpos'] == target
    assert rc._record_metadata()['reset_qpos'] == target


def test_reset_uses_bound_device_snapshot_and_zero_for_missing_pose(example_rc, example_mcp):
    rc = example_rc
    rc.config['reset_qpos'] = [9] * 14  # Legacy global settings do not override the device.
    profile, device = connector_profile(rc, 'station')
    config = json.loads(device.read_text())
    target = [i / 32 for i in range(14)]
    device.write_text(json.dumps({**config, 'reset_qpos': target}))
    command(rc, 'connector switch ' + profile)
    assert query(example_mcp, 'props')['reset_qpos'] == target
    device.write_text(json.dumps({**config, 'reset_qpos': [1, 2]}))
    command(rc, 'connector switch ' + profile, status='failed')
    assert rc.connector_properties()['reset_qpos'] == target
    device.write_text(json.dumps(config))
    command(rc, 'policy switch example --reset')
    np.testing.assert_array_equal(rc.robot.commands[-1], target)
    command(rc, 'connector switch ' + profile)
    assert query(example_mcp, 'props')['reset_qpos'] == [0] * 14
    command(rc, 'reset')
    np.testing.assert_array_equal(rc.robot.commands[-1], np.zeros(14))


@pytest.mark.parametrize('pose', [None, [0] * 13, [False] * 14, ['1'] * 14,
                                  [float('nan')] * 14, [1e39] * 14])
def test_invalid_device_pose_fails_before_connection(tmp_path, pose):
    robot, device = proxy(tmp_path, {})
    device.write_text(json.dumps({'reset_qpos': pose}))
    with pytest.raises(ValueError, match='reset_qpos'):
        robot.prepare()
    assert not robot._prepared and robot.process is None
