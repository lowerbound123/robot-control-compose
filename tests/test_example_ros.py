"""Optional real ROS transport with example as the only policy and no hardware."""
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from config_helpers import read_config
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from robot_control.controller import RobotController
from test_example_controller import ROOT, command
from test_ros_controller_integration import until
from test_ros_integration import _stop_simulator


def test_example_with_ros_feedback_cameras_and_commands(tmp_path, monkeypatch):
    pytest.importorskip("rclpy", reason="requires ROS 2 and the built test messages")
    pytest.importorskip("robot_control_test_msgs.msg")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    namespace = "/robot_control_test/example_" + uuid4().hex[:8]
    copy_templates(ROOT / "policy/example", tmp_path / "policy/example")
    config = read_config(ROOT / "configs/rc.ros-test.yaml.example")
    config.update(policy_directory="policy", startup_policy="example", outputs_directory="outputs",
                  timeouts=dict(initialize_s=10, inference_s=5, release_s=3))
    robot = read_config(ROOT / 'connector/acone/devices/ros-test.yaml.example')
    monkeypatch.setenv('ROS_DOMAIN_ID', str(robot['ros_domain_id']))
    old = robot["namespace"] + "/"
    robot["namespace"] = namespace
    for key in ("feedback_left_topic", "feedback_right_topic", "command_left_topic", "command_right_topic"):
        robot[key] = namespace + "/" + robot[key].removeprefix(old)
    robot["camera_topics"] = {name: namespace + "/" + topic.removeprefix(old)
                              for name, topic in robot["camera_topics"].items()}
    device = tmp_path / 'acone-device.yaml'
    device.write_text(json.dumps(robot))
    config['connectors']['body']['config'] = str(device)
    (tmp_path / "rc.yaml").write_text(json.dumps(config))
    with (tmp_path / "simulator.log").open("w") as log:
        sim = subprocess.Popen([sys.executable, "-m", "robot_control_test_sim.simulator",
                                "--namespace", namespace], stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
        try:
            rc = RobotController(tmp_path / "rc.yaml", process_factory=LocalPolicyProcess)
            rc.start()
            try:
                until(lambda: rc.status()["policy"] == "example" and rc.status()["state"] == "paused", rc)
                until(lambda: len(rc.robot.camera_preview_snapshot()) == 3, rc)
                command(rc, "resume")
                until(lambda: rc.status()["consumed"] >= 3, rc)
                command(rc, "pause")
                meta = json.loads(command(rc, "policy example example")["images"])
                assert set(meta) == {"head", "left_wrist", "right_wrist"}
                assert all(item["shape"] == [48, 64, 3] and item["readonly"] for item in meta.values())
                # A small observable move round-trips through both ROS publishers and feedback.
                state = rc.robot.read_state()["qpos"]
                target = [v + .03 if i in (0, 7) else v for i, v in enumerate(state)]
                pose = tmp_path / "pose.json"
                pose.write_text(json.dumps({"qpos": target}))
                command(rc, f'pose restore "{pose}"')
                until(lambda: all(abs(a-b) < .005 for a, b in zip(rc.robot.read_state()["qpos"], target)), rc)
            finally:
                rc.submit("exit")
                assert rc.done.wait(15) and rc.exit_code == 0 and rc._pool._bytes == 0, rc.read_events()
        finally:
            _stop_simulator(sim)
