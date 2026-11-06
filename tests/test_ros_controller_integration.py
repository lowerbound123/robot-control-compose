"""RC -> independent ACOne connector -> genuine ROS simulator; example only."""
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from config_helpers import read_config
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from robot_control.controller import RobotController
from test_ros_integration import _stop_simulator

ROOT = Path(__file__).resolve().parents[1]


def until(predicate, controller, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if controller.status()["state"] == "fault":
            pytest.fail(f"RC fault: {controller.status()}\n{list(controller.events.queue)}")
        time.sleep(.01)
    pytest.fail(f"ROS RC condition timed out: {controller.status()}\n{list(controller.events.queue)}")


@pytest.fixture
def ros_rc(tmp_path, monkeypatch, request):
    pytest.importorskip("rclpy", reason="requires ROS 2 and the built test messages")
    pytest.importorskip("robot_control_test_msgs.msg")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    namespace = "/robot_control_test/rc_" + uuid4().hex[:8]
    copy_templates(ROOT / "policy/example", tmp_path / "policy/example")
    manifest_path = tmp_path / "policy/example/policy.yaml"
    manifest = read_config(manifest_path)
    mode = getattr(request, "param", "async")
    manifest["execution"].update(mode=mode, call_every_actions=1)
    manifest["params"].update(chunk_size=32, inference_delay_s=.8)
    manifest_path.write_text(json.dumps(manifest))
    config = read_config(ROOT / "configs/rc.ros-test.yaml.example")
    config.update(policy_directory="policy", startup_policy="example", outputs_directory="outputs",
                  reset_qpos=[.3] * 14, timeouts=dict(initialize_s=10, inference_s=5, release_s=3))
    device = read_config(ROOT / "connector/acone/devices/ros-test.yaml.example")
    monkeypatch.setenv("ROS_DOMAIN_ID", str(device["ros_domain_id"]))
    old = device["namespace"] + "/"
    device["namespace"] = namespace
    for key in ("feedback_left_topic", "feedback_right_topic", "command_left_topic", "command_right_topic"):
        device[key] = namespace + "/" + device[key].removeprefix(old)
    device["camera_topics"] = {name: namespace + "/" + topic.removeprefix(old)
                              for name, topic in device["camera_topics"].items()}
    device_path = tmp_path / "device.yaml"
    device_path.write_text(json.dumps(device))
    config["connectors"]["body"]["config"] = str(device_path)
    path = tmp_path / "rc.yaml"
    path.write_text(json.dumps(config))
    rc = None
    with (tmp_path / "simulator.log").open("w") as log:
        simulator = subprocess.Popen([sys.executable, "-m", "robot_control_test_sim.simulator",
                                      "--namespace", namespace], stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        try:
            rc = RobotController(path, process_factory=LocalPolicyProcess)
            rc.start()
            until(lambda: rc.status()["policy"] == "example" and rc.status()["state"] == "paused", rc)
            yield rc, mode
        finally:
            if rc is not None:
                rc.submit("exit")
                assert rc.done.wait(15) and rc.exit_code == 0, rc.read_events()
                assert rc.robot.process.poll() == 0 and rc._pool._bytes == 0
            _stop_simulator(simulator)


@pytest.mark.parametrize("ros_rc", ["sync", "async"], indirect=True)
def test_actual_ros_slow_policy_retains_sync_and_async_behavior(ros_rc):
    rc, mode = ros_rc
    assert rc.wait_operation(rc.submit("resume"), 5)["status"] == "succeeded"
    until(lambda: rc.buffer.consumed >= 2 and rc.status()["in_flight"], rc)
    frames = rc.robot.read_images()
    assert all(frame.shape == (48, 64, 3) and not frame.flags.writeable for frame, _ in frames.values())
    time.sleep(.08)  # Allow receipts for earlier submissions to arrive.
    count, consumed = len(rc.robot.commands), rc.buffer.consumed
    time.sleep(.15)
    assert rc.status()["in_flight"]
    if mode == "sync":
        assert len(rc.robot.commands) == count and rc.buffer.consumed == consumed
    else:
        assert len(rc.robot.commands) > count and rc.buffer.consumed > consumed
    connector_pid = rc.robot.process.pid
    assert rc.wait_operation(rc.submit("pause"), 5)["status"] == "succeeded"
    assert rc.wait_operation(rc.submit("policy switch example"), 10)["status"] == "succeeded"
    assert rc.robot.process.pid == connector_pid and rc.robot.process.poll() is None


@pytest.mark.parametrize("motion", ["reset", "load"])
def test_actual_ros_exit_during_pose_movement(ros_rc, tmp_path, motion):
    rc, _ = ros_rc
    command = motion
    if motion == "load":
        path = tmp_path / "pose.json"
        path.write_text(json.dumps({"qpos": [.3] * 14}))
        command = f'pose restore "{path}"'
    rc.submit(command)
    until(lambda: rc.status()["state"] == "moving", rc)
    rc.submit("exit")
    assert rc.done.wait(15) and rc.exit_code == 0
    assert rc.robot.process.poll() == 0


def test_actual_ros_pose_save_reset_and_restore(ros_rc):
    rc, _ = ros_rc
    connector_pid = rc.robot.process.pid
    assert rc.wait_operation(rc.submit("pose save home"), 5)["status"] == "succeeded"
    saved_path = rc.outputs / "state_saves" / "home.json"
    saved = read_config(saved_path)["qpos"]
    consumed = rc.status()["consumed"]
    assert rc.wait_operation(rc.submit("reset"), 10)["status"] == "succeeded"
    until(lambda: np.allclose(rc.robot.read_state()["qpos"], rc.config["reset_qpos"], atol=.005), rc)
    assert rc.wait_operation(rc.submit("pose restore home"), 10)["status"] == "succeeded"
    until(lambda: np.allclose(rc.robot.read_state()["qpos"], saved, atol=.005), rc)
    assert rc.status()["consumed"] == consumed
    assert rc.robot.process.pid == connector_pid and rc.status()["state"] == "paused"
