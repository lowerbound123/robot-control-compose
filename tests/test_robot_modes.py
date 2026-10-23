import numpy as np
import pytest

from robot_control.robots import ACOneRobot, MockRobot
from test_robots import fake_ros as fake_ros, connect_fake, robot_config


def test_robot_cmd_uses_six_coordinates_and_separate_gripper(fake_ros):
    robot = ACOneRobot(robot_config())
    connect_fake(robot, fake_ros)
    try:
        robot.execute(np.arange(14))
        left, right = fake_ros.published["command_left"][-1], fake_ros.published["command_right"][-1]
        assert left.mode == right.mode == 5
        assert left.joint_pos == list(range(6)) and left.gripper == 6
        assert right.joint_pos == list(range(7, 13)) and right.gripper == 13
        robot.execute(np.arange(14), mode="epos")
        eef = fake_ros.published["command_left"][-1]
        assert eef.mode == 4 and eef.end_pos == list(range(6)) and eef.gripper == 6
        assert left.mode == 5  # published messages are never reused
    finally:
        robot.close()


def test_mock_partial_arm_command_holds_other_arm_and_tracks_modes():
    robot = MockRobot()
    robot.connect()
    before = robot.read_state()
    robot.execute_arms({"left": {"mode": "epos", "values": [.3, .2, .1, 0, 0, 0, -3.4]}})
    after = robot.read_state()
    assert after["eef"][:7] == pytest.approx([.3, .2, .1, 0, 0, 0, -3.4])
    assert after["qpos"][7:] == before["qpos"][7:]
    assert after["qpos"][6] == pytest.approx(-3.4)
    assert after["sequences"]["left"] > before["sequences"]["left"]
    robot.close()


def test_feedback_ready_does_not_require_images(fake_ros):
    robot = ACOneRobot(robot_config(camera_topics={"head": "camera"}))
    connect_fake(robot, fake_ros)
    try:
        assert len(robot.read_state()["qpos"]) == 14
        with pytest.raises(RuntimeError, match="camera"):
            robot.read_images()
        images, errors = robot.image_snapshot()
        assert images == {} and "head" in errors
    finally:
        robot.close()
