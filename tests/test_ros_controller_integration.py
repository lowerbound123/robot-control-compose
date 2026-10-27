"""Opt-in full RC / production Conda policy / genuine ROS simulator acceptance."""
from project_templates import template_path, copy_templates
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONDA = os.environ.get("RC_TEST_CONDA")
pytestmark = pytest.mark.skipif(
    not CONDA, reason="set RC_TEST_CONDA and source ROS/ros_ws for full RC ROS acceptance",
)


def until(predicate, controller, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if controller.status()["state"] == "fault":
            pytest.fail(f"RC fault: {controller.status()}\n{list(controller.events.queue)}")
        time.sleep(0.01)
    pytest.fail(f"ROS RC condition timed out: {controller.status()}\n{list(controller.events.queue)}")


class RosRig:
    def __init__(self, controller, command_times):
        self.controller = controller
        self.command_times = command_times
        self.workers = []
        self.image_directory = controller._pool.directory

    def remember_worker(self):
        self.workers.append(self.controller._worker)
        return self.workers[-1]

    def stop(self):
        self.controller.submit("exit")
        assert self.controller.done.wait(20), self.controller.status()
        self.controller._thread.join(1)
        assert self.controller.exit_code == 0, list(self.controller.events.queue)
        assert not self.controller._thread.is_alive()
        assert not self.controller._manager.is_alive()
        assert self.controller.robot.closed
        assert not self.controller.robot._spin_thread.is_alive()
        assert not self.controller.robot._decoder_thread.is_alive()
        assert not self.image_directory.exists()
        for worker in self.workers:
            assert not worker.alive
            with pytest.raises(ProcessLookupError):
                os.killpg(worker.pid, 0)
            assert not worker._directory.exists()
        # Drain already queued DDS deliveries, then prove publication stops.
        time.sleep(0.15)
        count = len(self.command_times)
        time.sleep(0.2)
        assert len(self.command_times) == count


@pytest.fixture
def ros_rc(tmp_path, monkeypatch):
    rclpy = pytest.importorskip("rclpy", reason="source ROS 2 Humble")
    messages = pytest.importorskip("robot_control_test_msgs.msg", reason="build/source ros_ws")
    assert importlib.util.find_spec("robot_control_test_sim.simulator")
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from robot_control.controller import RobotController

    monkeypatch.setenv("ROS_DOMAIN_ID", os.environ.get("ROS_DOMAIN_ID", "83"))
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    namespace = "/robot_control_test/rc_" + uuid.uuid4().hex[:8]
    policies = tmp_path / "policy"
    copy_templates(ROOT / "policy", policies)
    manifest_path = policies / "delayed_hold" / "policy.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["params"]["delay_s"] = 0.8
    manifest_path.write_text(json.dumps(manifest))
    proof = tmp_path / "policy-proof.json"
    # The probe executes inside the isolated policy interpreter on real frames.
    policy_path = policies / "hold" / "main.py"
    policy_path.write_text(policy_path.read_text() + '''
import importlib.util, json, sys
from pathlib import Path
_original_policy = policy
def policy(context, state, action_chunk, images, config):
    assert importlib.util.find_spec("robot_control") is None
    assert importlib.util.find_spec("textual") is None
    assert importlib.util.find_spec("rclpy") is None
    assert len(state["qpos"]) == len(state["qvel"]) == len(state["effort"]) == 14
    assert "timestamps" in state and "raw" in state
    for index, camera in enumerate(("head", "left_wrist", "right_wrist")):
        frame = images[camera]
        assert frame.shape == (48, 64, 3) and not frame.flags.writeable
        assert frame[0, 0, index] == 220
    path = Path(config["params"]["proof_path"])
    if not path.exists():
        path.write_text(json.dumps({"prefix":sys.prefix, "images":sorted(images)}))
    return _original_policy(context, state, action_chunk, images, config)
''')
    hold_manifest = policies / "hold" / "policy.json"
    hold_config = json.loads(hold_manifest.read_text())
    hold_config["params"]["proof_path"] = str(proof)
    hold_manifest.write_text(json.dumps(hold_config))
    config = json.loads((template_path(ROOT / "configs" / "rc.ros-test.json")).read_text())
    robot_config = config["robot"]
    robot_config["namespace"] = namespace
    for key in ("feedback_left_topic", "feedback_right_topic", "command_left_topic", "command_right_topic"):
        robot_config[key] = robot_config[key].replace("/robot_control_test", namespace, 1)
    robot_config["camera_topics"] = {
        camera: topic.replace("/robot_control_test", namespace, 1)
        for camera, topic in robot_config["camera_topics"].items()
    }
    config.update(conda_executable=CONDA, policy_directory=str(policies),
                  outputs_directory=str(tmp_path / "outputs"), reset_qpos=np.linspace(-0.15, 0.15, 14).tolist())
    config["timeouts"] = {"initialize_s": 15, "inference_s": 5, "release_s": 3}
    path = tmp_path / "rc.json"
    path.write_text(json.dumps(config))

    context = Context()
    rclpy.init(args=[], context=context)
    observer = Node("rc_command_observer", namespace=namespace, context=context)
    command_times = []
    for topic in (robot_config["command_left_topic"], robot_config["command_right_topic"]):
        observer.create_subscription(messages.ArmCommand, topic, lambda _: command_times.append(time.monotonic()), 10)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(observer)
    observing = threading.Event()
    observing.set()

    def spin():
        while observing.is_set():
            executor.spin_once(timeout_sec=0.05)

    thread = threading.Thread(target=spin, name="test-ros-observer")
    thread.start()
    log = (tmp_path / "simulator.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "robot_control_test_sim.simulator", "--namespace", namespace],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    rig = None
    try:
        controller = RobotController(path)
        rig = RosRig(controller, command_times)
        controller.start()
        until(lambda: controller.status()["state"] == "paused" and controller.status()["policy"] == "hold", controller)
        rig.remember_worker()
        rig.proof = proof
        yield rig
    finally:
        try:
            if rig is not None:
                rig.stop()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            log.close()
            observing.clear()
            executor.wake()
            thread.join(2)
            assert not thread.is_alive()
            executor.shutdown(timeout_sec=2)
            observer.destroy_node()
            rclpy.try_shutdown(context=context)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_actual_ros_rc_policy_images_pose_switch_and_exit(ros_rc, tmp_path, mode):
    controller = ros_rc.controller
    controller.submit("resume")
    until(lambda: controller.status()["consumed"] >= 4 and ros_rc.proof.exists(), controller)
    proof = json.loads(ros_rc.proof.read_text())
    assert Path(proof["prefix"]).name == "rc-policy-demo"
    assert Path(proof["prefix"]).resolve() != Path(sys.prefix).resolve()
    assert proof["images"] == ["head", "left_wrist", "right_wrist"]
    assert ros_rc.image_directory.parent == Path("/dev/shm")

    controller.submit("save home")
    saved_path = controller.outputs / "state_saves" / "home.json"
    until(lambda: saved_path.exists() and controller.status()["hold_name"] == "home", controller)
    saved = json.loads(saved_path.read_text())["qpos"]
    pose_consumed = controller.status()["consumed"]
    controller.submit("reset")
    until(lambda: controller.status()["state"] == "paused" and controller.status()["hold_name"] == "reset", controller)
    until(lambda: np.allclose(controller.robot.read_state()["qpos"], controller.config["reset_qpos"], atol=0.005), controller)
    controller.submit("load home")
    until(lambda: controller.status()["state"] == "paused" and controller.status()["hold_name"] == "home", controller)
    until(lambda: np.allclose(controller.robot.read_state()["qpos"], saved, atol=0.005), controller)
    assert controller.status()["consumed"] == pose_consumed

    old = ros_rc.workers[-1]
    controller.submit("switch delayed_hold")
    until(lambda: controller.status()["policy"] == "delayed_hold" and controller.status()["state"] == "paused", controller)
    ros_rc.remember_worker()
    with pytest.raises(ProcessLookupError):
        os.killpg(old.pid, 0)
    controller.submit(f'set policy.execution.mode "{mode}"')
    until(lambda: controller.status()["config_version"] == 2, controller)
    before = controller.status()["consumed"]
    controller.submit("resume")
    until(lambda: controller.status()["consumed"] >= before + 2 and controller.status()["in_flight"], controller)
    time.sleep(0.1)
    count = len(ros_rc.command_times)
    consumed = controller.status()["consumed"]
    time.sleep(0.15)
    assert controller.status()["in_flight"]
    if mode == "sync":
        assert len(ros_rc.command_times) == count
        assert controller.status()["consumed"] == consumed
    else:
        assert len(ros_rc.command_times) > count
        assert controller.status()["consumed"] > consumed
    ros_rc.stop()


@pytest.mark.parametrize("motion", ["reset", "load"])
def test_actual_ros_exit_during_pose_movement(ros_rc, tmp_path, motion):
    controller = ros_rc.controller
    if motion == "load":
        path = tmp_path / "legacy-pose.json"
        path.write_text(json.dumps({"qpos": [0.3] * 14}))
        command = "load " + json.dumps(str(path))
    else:
        command = "reset"
    controller.submit(command)
    until(lambda: controller.status()["state"] == "moving", controller)
    ros_rc.stop()
