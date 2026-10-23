"""Genuine ROS transport test; skipped when ROS or generated msgs are missing.

Run under ROS's Python after sourcing ROS and ros_ws/install/setup.bash.
This test starts an independent simulator process; no fake ROS binding is used.
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest

from robot_control.robots import ACOneRobot


def _stop_simulator(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_actual_ros_closed_loop_cameras_staleness_and_shutdown(tmp_path, monkeypatch):
    # These checks distinguish a missing runtime from test failures on a
    # configured ROS machine. A sourced but broken simulator must fail.
    pytest.importorskip("rclpy", reason="ROS 2 Python bindings missing; manually install/source ROS in Orb")
    pytest.importorskip("sensor_msgs.msg", reason="source ROS 2 before running this integration test")
    pytest.importorskip(
        "robot_control_test_msgs.msg", reason="build ros_ws with colcon and source install/setup.bash",
    )
    assert importlib.util.find_spec("robot_control_test_sim.simulator"), "source ros_ws/install/setup.bash"
    monkeypatch.setenv("ROS_DOMAIN_ID", os.environ.get("ROS_DOMAIN_ID", "83"))
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    namespace = "/robot_control_test/run_" + uuid.uuid4().hex[:8]
    topics = lambda suffix: namespace + "/" + suffix
    robot = ACOneRobot({
        "message_type": "robot_control_test_msgs/msg/ArmState", "namespace": namespace,
        "command_message_type": "robot_control_test_msgs/msg/ArmCommand",
        "feedback_left_topic": topics("feedback/left"), "feedback_right_topic": topics("feedback/right"),
        "command_left_topic": topics("command/left"), "command_right_topic": topics("command/right"),
        "camera_topics": {camera: topics(f"camera/{camera}/compressed") for camera in ("head", "left_wrist", "right_wrist")},
        "feedback_timeout_s": 0.8, "camera_timeout_s": 0.8, "connect_timeout_s": 15,
    })
    log_path = tmp_path / "simulator.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "robot_control_test_sim.simulator", "--namespace", namespace],
            env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            try:
                robot.connect()
            except Exception as error:
                pytest.fail(f"ROS adapter did not connect: {error}\nSimulator log:\n{log_path.read_text()}")
            state = robot.read_state()
            assert len(state["qpos"]) == len(state["eef"]) == 14
            assert len(state["raw"]["left"]["joint_pos"]) == 7
            json.dumps(state, allow_nan=False)
            # connect() waits for arm feedback, not asynchronous camera decode.
            deadline=time.monotonic()+3
            while True:
                try:
                    images = robot.read_images()
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(.01)
            assert set(images) == {"head", "left_wrist", "right_wrist"}
            for index, camera in enumerate(("head", "left_wrist", "right_wrist")):
                frame, timestamp = images[camera]
                assert frame.shape == (48, 64, 3)
                assert frame.dtype == np.uint8 and not frame.flags.writeable
                assert frame[0, 0, index] > 180  # RGB channel identity survives compression.
                assert timestamp > 0
            target = np.linspace(-0.4, 0.4, 14)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                robot.execute(target)
                if np.allclose(robot.read_state()["qpos"], target, atol=0.005):
                    break
                assert process.poll() is None, log_path.read_text()
                time.sleep(0.03)
            else:
                pytest.fail(f"ROS feedback never reached command; last feedback={robot.read_state()['qpos']}")
            _stop_simulator(process)
            time.sleep(1.0)
            with pytest.raises(RuntimeError, match="stale.*feedback"):
                robot.read_state()
            with pytest.raises(RuntimeError, match="stale.*camera"):
                robot.read_images()
        finally:
            _stop_simulator(process)
            robot.close()
            robot.close()
        assert not robot._spin_thread.is_alive()
        assert not robot._decoder_thread.is_alive()
        assert process.poll() is not None
